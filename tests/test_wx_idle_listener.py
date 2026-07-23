"""T5 (wx port of T3): resident idle listener drains unsolicited turns between
sends.

Mock at the provider boundary (poll_line + recv yield dicts); never spawn
claude, never touch real WeChat. Provider death surfaces as POLL_EOF; the
listener never dies from an exception and re-reads self._provider every call.
TypingPing is stubbed so no real re-ping thread / real sleep runs.
"""

from __future__ import annotations

import json

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_core.providers.cc import POLL_EOF
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.config import Config
from synapse_wx.loop import MainLoop


class FakeILink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.typing = 0

    def send_text(self, to_user_id, ctx_token, text, **_kwargs) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    def send_typing(self, *_a, **_k) -> None:
        self.typing += 1


class _StubTyping:
    """No-op typing indicator; fires one ping on start so tests can assert
    typing ran. No background thread / real sleep (mirrors the tg lesson)."""

    def __init__(self, ilink, to_user_id, context_token, interval=5.0) -> None:
        self._ilink = ilink
        self.started = False

    def start(self) -> None:
        self.started = True
        try:
            self._ilink.send_typing(None, None)
        except Exception:
            pass

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def stub_typing(monkeypatch):
    monkeypatch.setattr("synapse_wx.loop.TypingPing", _StubTyping)


@pytest.fixture(autouse=True)
def one_bubble_split(monkeypatch):
    monkeypatch.setattr(
        "synapse_wx.loop.split_for_wechat_typed",
        lambda text: [{"kind": "text", "text": text}],
    )


def _turn_lines(text, *, unsolicited=True, sid="sid-x"):
    lines = []
    if unsolicited:
        lines.append(json.dumps({"type": "system", "subtype": "task_notification"}))
    lines.append(json.dumps({"type": "system", "subtype": "init", "session_id": sid}))
    lines.append(json.dumps({"type": "assistant",
                             "message": {"content": [{"type": "text", "text": text}]}}))
    lines.append(json.dumps({"type": "result", "result": text}))
    return lines


class QueueProvider:
    """poll_line pops one line; recv(first_line) consumes first_line then the
    queue until a result. Scripts a list of raw lines + POLL_EOF sentinels."""

    def __init__(self, lines: list) -> None:
        self._lines = list(lines)
        self.alive = True
        self.session_id = None
        self.turn_output_capped = False
        self.usage_total: dict = {}

    def poll_line(self, timeout):
        if not self._lines:
            return None
        item = self._lines.pop(0)
        if item is POLL_EOF:
            self.alive = False
            return POLL_EOF
        return item

    def recv(self, first_line=None):
        if first_line is not None:
            ev0 = json.loads(first_line)
            yield ev0
            if ev0.get("type") == "result":
                return
        while self._lines:
            item = self._lines.pop(0)
            if item is POLL_EOF:
                return
            ev = json.loads(item)
            yield ev
            if ev.get("type") == "result":
                return

    def send(self, msg):
        return None

    def is_alive(self):
        return self.alive


def _loop(tmp_path, alerts=None) -> MainLoop:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    ilink = FakeILink()
    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda *_a, **_k: None,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.01,
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        alerts=alerts,
        cfg=Config(),
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        media_dir=tmp_path / "media",
    )
    loop._last_from_wxid = "lumi"
    loop._last_ctx_token = "ctx-1"
    return loop


def test_idle_unsolicited_delivered_without_inbound(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = QueueProvider(_turn_lines("background answer"))
    loop._listen_once()
    assert [s[2] for s in loop._ilink.sent] == ["background answer"]
    # typing indicator ran during generation
    assert loop._ilink.typing >= 1


def test_idle_none_poll_is_noop(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = QueueProvider([])  # poll_line -> None
    loop._listen_once()
    assert loop._ilink.sent == []


def test_poll_eof_marks_dead_no_respawn(tmp_path):
    loop = _loop(tmp_path)
    prov = QueueProvider([POLL_EOF])
    loop._provider = prov
    loop._listen_once()
    assert prov.alive is False
    # Listener does NOT respawn; provider object stays (lazy respawn on send).
    assert loop._provider is prov
    assert loop._ilink.sent == []


def test_consecutive_back_to_back_turns(tmp_path):
    loop = _loop(tmp_path)
    lines = _turn_lines("bg one") + _turn_lines("bg two")
    loop._provider = QueueProvider(lines)
    loop._listen_once()
    assert [s[2] for s in loop._ilink.sent] == ["bg one", "bg two"]


def test_no_chat_target_drops_with_warning_no_crash(tmp_path):
    loop = _loop(tmp_path)
    loop._last_from_wxid = None
    loop._provider = QueueProvider(_turn_lines("orphan"))
    # Must not raise; turn drained, nothing sent (no chat target).
    loop._listen_once()
    assert loop._ilink.sent == []


def test_dead_provider_is_noop(tmp_path):
    loop = _loop(tmp_path)
    prov = QueueProvider(_turn_lines("x"))
    prov.alive = False
    loop._provider = prov
    loop._listen_once()
    assert loop._ilink.sent == []


def test_none_provider_is_noop(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = None
    loop._listen_once()  # no crash
    assert loop._ilink.sent == []


def test_exception_in_delivery_does_not_kill_listener(tmp_path):
    """A delivery blow-up in one iteration is caught by _idle_listener's
    catch-all; the loop keeps running and exits cleanly on the stop event."""
    loop = _loop(tmp_path)
    loop._provider = QueueProvider(_turn_lines("boom"))

    def bad_deliver(*a, **k):
        raise RuntimeError("delivery blew up")

    loop._deliver_reply = bad_deliver  # type: ignore[assignment]

    # Drive one guarded iteration then stop: _idle_listener must return without
    # the exception escaping.
    calls = {"n": 0}
    orig_listen = loop._listen_once

    def guarded_listen():
        calls["n"] += 1
        try:
            orig_listen()
        finally:
            loop._stop_evt.set()

    loop._listen_once = guarded_listen  # type: ignore[assignment]
    loop._idle_listener()
    assert calls["n"] == 1


def test_provider_swapped_mid_poll_picked_up_next_iteration(tmp_path):
    """The listener must re-read self._provider inside the lock. Swap the
    provider before the iteration; the new object is used, no crash."""
    loop = _loop(tmp_path)
    loop._provider = QueueProvider([])
    # Simulate a swap: replace with a fresh provider that has a real turn.
    loop._provider = QueueProvider(_turn_lines("after swap"))
    loop._listen_once()
    assert [s[2] for s in loop._ilink.sent] == ["after swap"]
