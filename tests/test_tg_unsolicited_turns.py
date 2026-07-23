"""T2/T4: turn-aware _stream_response — unsolicited (background-task) turns
deliver inline before the solicited reply, shared delivery path, storm alert.

Mock at the provider boundary (recv yields dict events); never spawn claude.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class RecordingAlerts:
    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, severity, kind, message, source="", *, fingerprint=None):
        self.written.append(
            {"severity": severity, "kind": kind, "message": message,
             "source": source, "fingerprint": fingerprint}
        )
        return Path("/dev/null")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return type("M", (), {"message_id": len(self.sent)})()

    async def send_chat_action(self, **_):
        return None


class ScriptedProvider:
    """recv() yields the events of ONE turn per call, walking a script of
    turns. Each turn is a list of dict events ending in a result."""

    def __init__(self, turns: list[list[dict]]) -> None:
        self._turns = list(turns)
        self.alive = True
        self.session_id = None
        self.turn_output_capped = False

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


class _NoTyping:
    running = True

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def fake_sleep(_s):
        return None
    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)


def _loop(tmp_path, alerts=None, storm_cap=5):
    cfg = TgConfig(data_dir=tmp_path / "tg-data", unsolicited_storm_cap=storm_cap)
    loop = TgLoop(cfg, alerts=alerts)
    loop._pending_chat_id = 123
    return loop


def _stream(loop, bot, provider, monkeypatch):
    loop._provider = provider
    monkeypatch.setattr(
        "synapse_tg.loop.split_for_tg_typed",
        lambda text: [{"kind": "text", "text": text}],
    )
    monkeypatch.setattr("synapse_tg.loop.gfm_to_tg_html", lambda t: t)
    return asyncio.run(loop._stream_response(bot, 123, _NoTyping()))


def test_solicited_only_one_delivery_unchanged(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_turn("hello")])
    text, thinking = _stream(loop, bot, provider, monkeypatch)
    assert text == "hello"
    # _stream_response returns the solicited reply; it is NOT delivered here
    # (check_flush delivers it). No stray sends from _stream_response.
    assert bot.sent == []


def test_unsolicited_before_solicited_delivers_first(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn("background done", unsolicited=True),
        _turn("real reply"),
    ])
    text, thinking = _stream(loop, bot, provider, monkeypatch)
    # Unsolicited text delivered inline first.
    assert [m["text"] for m in bot.sent] == ["background done"]
    # Solicited reply returned for check_flush to send.
    assert text == "real reply"


def test_consecutive_unsolicited_turns(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn("bg one", unsolicited=True),
        _turn("bg two", unsolicited=True),
        _turn("solicited"),
    ])
    text, _ = _stream(loop, bot, provider, monkeypatch)
    assert [m["text"] for m in bot.sent] == ["bg one", "bg two"]
    assert text == "solicited"


def test_notification_frame_yields_no_text(tmp_path, monkeypatch):
    """A turn opening with task_notification is classified unsolicited and the
    notification frame produces no text (only the assistant text ships)."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn("only real text", unsolicited=True),
        _turn("reply"),
    ])
    text, _ = _stream(loop, bot, provider, monkeypatch)
    assert [m["text"] for m in bot.sent] == ["only real text"]
    assert text == "reply"


def test_storm_alert_fires_once_at_cap_plus_one(tmp_path, monkeypatch):
    alerts = RecordingAlerts()
    loop = _loop(tmp_path, alerts=alerts, storm_cap=2)
    bot = FakeBot()
    # 3 unsolicited (> cap 2) then the solicited reply.
    provider = ScriptedProvider([
        _turn("u1", unsolicited=True),
        _turn("u2", unsolicited=True),
        _turn("u3", unsolicited=True),
        _turn("done"),
    ])
    text, _ = _stream(loop, bot, provider, monkeypatch)
    # All unsolicited turns still delivered.
    assert [m["text"] for m in bot.sent] == ["u1", "u2", "u3"]
    assert text == "done"
    storm = [a for a in alerts.written if a["kind"] == "bridge_turn_storm"]
    assert len(storm) == 1
    assert storm[0]["fingerprint"] == "bridge_turn_storm"


def test_storm_cap_default_when_absent():
    from synapse_tg.config import TgConfig, load_config
    assert TgConfig().unsolicited_storm_cap == 5


def test_storm_cap_config_override(tmp_path):
    from synapse_tg.config import load_config
    p = tmp_path / "config.toml"
    p.write_text("[provider]\nunsolicited_storm_cap = 3\n")
    cfg = load_config(p)
    assert cfg.unsolicited_storm_cap == 3
