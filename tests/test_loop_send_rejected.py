"""Outbound send rejection: a False send_text return stops the remaining
bubbles of the turn and raises a wx_send_rejected alert.

Also pins config-first bubble pacing (bubble_gap_sec from Config).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_core.providers.base import Provider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.config import Config
from synapse_wx.loop import _DEFAULT_BUBBLE_GAP_SEC, MainLoop


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class RejectingILink:
    """send_text returns False from the Nth call onward (1-indexed)."""

    def __init__(self, fail_from: int = 1) -> None:
        self._fail_from = fail_from
        self._calls = 0
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        return []

    def send_text(self, to_user_id: str, ctx_token: str, text: str, **_) -> bool:
        self._calls += 1
        self.sent.append((to_user_id, ctx_token, text))
        return self._calls < self._fail_from

    def send_typing(self, *a, **k) -> None:
        return None

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class RecordingAlerts:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    def write(self, severity, kind, message, source="", *, fingerprint=None):
        self.writes.append(
            {
                "severity": severity,
                "kind": kind,
                "message": message,
                "source": source,
                "fingerprint": fingerprint,
            }
        )
        return Path("/tmp/fake_alert.txt")


class MultiBubbleProvider(Provider):
    """Yields a reply that splits into several text bubbles (newline-separated)."""

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self._alive = True

    def spawn(self, env=None) -> None:
        self._alive = True

    def send(self, prompt: str) -> None:
        return None

    def recv(self):
        yield {"type": "system", "subtype": "init", "session_id": "sid-reject"}
        yield {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": self._reply}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        }
        yield {"type": "result", "result": self._reply, "session_id": "sid-reject"}

    def close(self) -> None:
        self._alive = False

    def cancel(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture()
def env(tmp_path: Path):
    return {
        "state": BridgeState(),
        "sessions": SessionTracker(state_path=tmp_path / "sessions.json"),
        "tmp": tmp_path,
    }


def _make_loop(env, ilink, provider, *, alerts=None, cfg=None) -> MainLoop:
    clock = FakeClock()
    fixed = datetime(2026, 6, 12, 12, 0)
    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
        alerts=alerts,
        cfg=cfg,
    )
    loop._provider = provider
    loop._provider.spawn()
    with loop._state_lock:
        loop._buffer.add("go")
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"
    clock.advance(6.0)
    return loop, clock


def test_false_send_stops_remaining_bubbles_and_alerts(env) -> None:
    # 3 bubbles; the 2nd send_text is rejected.
    ilink = RejectingILink(fail_from=2)
    alerts = RecordingAlerts()
    provider = MultiBubbleProvider("bubble one\n\nbubble two\n\nbubble three")
    loop, _ = _make_loop(env, ilink, provider, alerts=alerts)

    loop.maybe_flush()

    # bubble 1 sent, bubble 2 attempted (rejected), bubble 3 never attempted.
    assert len(ilink.sent) == 2
    assert ilink.sent[0][2] == "bubble one"
    assert ilink.sent[1][2] == "bubble two"
    # Exactly one wx_send_rejected alert, reporting 2 lost bubbles (2/3 + 3).
    rejects = [w for w in alerts.writes if w["kind"] == "wx_send_rejected"]
    assert len(rejects) == 1
    assert rejects[0]["fingerprint"] == "wx.send_rejected"
    assert "2" in rejects[0]["message"]


def test_false_send_without_alert_sink_is_safe(env) -> None:
    ilink = RejectingILink(fail_from=1)
    provider = MultiBubbleProvider("only one\n\nsecond")
    loop, _ = _make_loop(env, ilink, provider, alerts=None)
    # Must not raise even though _alerts is None.
    loop.maybe_flush()
    assert len(ilink.sent) == 1


def test_bubble_gap_comes_from_config(env) -> None:
    cfg = Config()
    cfg.bubble_gap_sec = 1.7
    ilink = RejectingILink(fail_from=99)  # never fails
    provider = MultiBubbleProvider("a")
    loop, _ = _make_loop(env, ilink, provider, cfg=cfg)
    assert loop._bubble_gap_sec == 1.7


def test_bubble_gap_default_when_no_cfg(env) -> None:
    ilink = RejectingILink(fail_from=99)
    provider = MultiBubbleProvider("a")
    loop, _ = _make_loop(env, ilink, provider, cfg=None)
    assert loop._bubble_gap_sec == _DEFAULT_BUBBLE_GAP_SEC
