"""T5 (wx port of T2/T4): turn-aware _drain_recv — unsolicited (background-task)
turns deliver inline before the solicited reply via the shared _deliver_reply
path; storm alert fires once at cap+1.

Mock at the provider boundary (recv yields dict events); never spawn claude,
never touch real WeChat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.config import Config
from synapse_wx.loop import MainLoop


class RecordingAlerts:
    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, severity, kind, message, source="", *, fingerprint=None):
        self.written.append(
            {"severity": severity, "kind": kind, "message": message,
             "source": source, "fingerprint": fingerprint}
        )
        return Path("/dev/null")


class FakeILink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send_text(self, to_user_id, ctx_token, text, **_kwargs) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    def send_typing(self, *_a, **_k) -> None:
        return None


class ScriptedProvider:
    """recv() yields the events of ONE turn per call, walking a script of
    turns. Each turn is a list of dict events ending in a result."""

    def __init__(self, turns: list[list[dict]]) -> None:
        self._turns = list(turns)
        self.alive = True
        self.session_id = None
        self.turn_output_capped = False
        self.usage_total: dict = {}

    def recv(self, first_line=None):
        if not self._turns:
            return
        for ev in self._turns.pop(0):
            yield ev

    def send(self, msg):
        return None

    def is_alive(self):
        return True


def _turn(text, *, unsolicited=False, sid="sid-x"):
    evs = []
    if unsolicited:
        evs.append({"type": "system", "subtype": "task_notification"})
    evs.append({"type": "system", "subtype": "init", "session_id": sid})
    evs.append({"type": "assistant",
                "message": {"content": [{"type": "text", "text": text}],
                            "usage": {"output_tokens": 1}}})
    evs.append({"type": "result", "result": text})
    return evs


@pytest.fixture(autouse=True)
def one_bubble_split(monkeypatch):
    monkeypatch.setattr(
        "synapse_wx.loop.split_for_wechat_typed",
        lambda text: [{"kind": "text", "text": text}],
    )


def _loop(tmp_path, alerts=None, storm_cap=5) -> MainLoop:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    ilink = FakeILink()
    cfg = Config(unsolicited_storm_cap=storm_cap)
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
        cfg=cfg,
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        media_dir=tmp_path / "media",
    )
    loop._last_from_wxid = "lumi"
    loop._last_ctx_token = "ctx-1"
    return loop


def test_solicited_only_one_reply_unchanged(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = ScriptedProvider([_turn("hello")])
    text = loop._drain_recv()
    assert text == "hello"
    # _drain_recv returns the solicited reply; maybe_flush delivers it. No
    # stray sends from _drain_recv itself.
    assert loop._ilink.sent == []


def test_unsolicited_before_solicited_delivers_first(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = ScriptedProvider([
        _turn("background done", unsolicited=True),
        _turn("real reply"),
    ])
    text = loop._drain_recv()
    # Unsolicited text delivered inline first (via _deliver_reply → send_text).
    assert [s[2] for s in loop._ilink.sent] == ["background done"]
    # Solicited reply returned for maybe_flush to send.
    assert text == "real reply"


def test_consecutive_unsolicited_turns(tmp_path):
    loop = _loop(tmp_path)
    loop._provider = ScriptedProvider([
        _turn("bg one", unsolicited=True),
        _turn("bg two", unsolicited=True),
        _turn("solicited"),
    ])
    text = loop._drain_recv()
    assert [s[2] for s in loop._ilink.sent] == ["bg one", "bg two"]
    assert text == "solicited"


def test_notification_frame_yields_no_text(tmp_path):
    """A turn opening with task_notification is classified unsolicited and the
    notification frame produces no text (only the assistant text ships)."""
    loop = _loop(tmp_path)
    loop._provider = ScriptedProvider([
        _turn("only real text", unsolicited=True),
        _turn("reply"),
    ])
    text = loop._drain_recv()
    assert [s[2] for s in loop._ilink.sent] == ["only real text"]
    assert text == "reply"


def test_storm_alert_fires_once_at_cap_plus_one(tmp_path):
    alerts = RecordingAlerts()
    loop = _loop(tmp_path, alerts=alerts, storm_cap=2)
    loop._provider = ScriptedProvider([
        _turn("u1", unsolicited=True),
        _turn("u2", unsolicited=True),
        _turn("u3", unsolicited=True),
        _turn("done"),
    ])
    text = loop._drain_recv()
    # All unsolicited turns still delivered.
    assert [s[2] for s in loop._ilink.sent] == ["u1", "u2", "u3"]
    assert text == "done"
    storm = [a for a in alerts.written if a["kind"] == "bridge_turn_storm"]
    assert len(storm) == 1
    assert storm[0]["fingerprint"] == "bridge_turn_storm"


def test_storm_cap_default_when_absent():
    assert Config().unsolicited_storm_cap == 5


def test_storm_cap_config_override(tmp_path):
    from synapse_wx.config import load_config
    p = tmp_path / "config.toml"
    p.write_text("[provider]\nunsolicited_storm_cap = 3\n")
    cfg = load_config(p)
    assert cfg.unsolicited_storm_cap == 3
