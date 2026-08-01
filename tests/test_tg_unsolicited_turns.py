"""T2/T4: turn-aware _stream_response — unsolicited (background-task) turns
deliver inline before the solicited reply, shared delivery path, storm alert.

Mock at the provider boundary (recv yields dict events); never spawn claude.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
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


def _turn_with_tool_use(tool_input, *, sid="sid-x", name="mcp__marrow__lie_down",
                        text="ok"):
    content = [{"type": "tool_use", "name": name, "input": tool_input}]
    if text:
        content.append({"type": "text", "text": text})
    evs = [
        {"type": "system", "subtype": "init", "session_id": sid},
        {"type": "assistant", "message": {
            "content": content, "usage": {"output_tokens": 1}}},
        {"type": "result", "result": text},
    ]
    return evs


class _NoTyping:
    running = True

    def start(self):
        pass

    def stop(self):
        pass


_REAL_SLEEP = asyncio.sleep


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def fake_sleep(_s):
        return None
    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)


@pytest.fixture
def real_sleep(monkeypatch):
    """Undo no_real_sleep: the send gap must actually yield, so a rotate
    waiting on the lock gets to run while the reply is still shipping."""
    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", _REAL_SLEEP)


def _loop(tmp_path, alerts=None, storm_cap=5, shells=("cli", "tg")):
    # marrow config dir = parent of marrow_db; shell_peer() reads
    # [cortex].shells from there, so it must never resolve to the live dir.
    (tmp_path / "config.toml").write_text(
        "[cortex]\nshells = [%s]\n" % ", ".join(f'"{s}"' for s in shells)
    )
    cfg = TgConfig(data_dir=tmp_path / "tg-data",
                   marrow_db=str(tmp_path / "marrow.db"),
                   unsolicited_storm_cap=storm_cap)
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


# ── shell receipts (💤 / 🌙 / 🔄): queued during the turn, shipped after it ────

class _FixedClock:
    """datetime subclass pinned to a fixed instant, tz kept as passed."""
    def __init__(self, y, mo, d, h, mi):
        self._parts = (y, mo, d, h, mi)

    def install(self, monkeypatch, module):
        y, mo, d, h, mi = self._parts

        class _Fixed(module.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(y, mo, d, h, mi, tzinfo=tz)

        monkeypatch.setattr(module, "datetime", _Fixed)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _NoSpawnProvider:
    """ClaudeCodeProvider stand-in at the spawn boundary — a respawn inside a
    rotate must never start a cc process."""

    alive = True
    session_id = None
    turn_output_capped = False

    def __init__(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def is_alive(self):
        return True

    def spawn(self):
        pass

    def cancel(self):
        pass

    def close(self):
        pass


@pytest.fixture
def no_spawn(monkeypatch):
    monkeypatch.setattr("synapse_tg.loop.ClaudeCodeProvider", _NoSpawnProvider)


class _StubTyping:
    running = True

    def __init__(self, bot, chat_id) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _arm_cycle(loop, bot, provider, monkeypatch, body="hi"):
    """Wire one ready reply cycle: scripted provider, buffered body, no real
    bubble splitting/typing. Returns the context check_flush takes."""
    loop._provider = provider
    clock = _FakeClock()
    loop._buffer = InboundBuffer(clock=clock)
    loop._buffer.add(body)
    clock.t += InboundBuffer.HOLD_QUIET_SEC + 1.0
    monkeypatch.setattr("synapse_tg.loop.TypingAction", _StubTyping)
    monkeypatch.setattr(
        "synapse_tg.loop.split_for_tg_typed",
        lambda text: [{"kind": "text", "text": text}],
    )
    monkeypatch.setattr("synapse_tg.loop.gfm_to_tg_html", lambda t: t)
    return type("Ctx", (), {"bot": bot})()


def _cycle(loop, bot, provider, monkeypatch, body="hi"):
    """Run one full reply cycle (check_flush) and return the texts sent."""
    ctx = _arm_cycle(loop, bot, provider, monkeypatch, body)
    asyncio.run(loop.check_flush(ctx))
    return [m["text"] for m in bot.sent]


def test_lie_down_notice_lands_under_the_reply_text(tmp_path, monkeypatch):
    import synapse_tg.loop as mod
    _FixedClock(2026, 7, 26, 10, 0).install(monkeypatch, mod)
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_turn_with_tool_use({"next_wake_min": 30})])
    assert _cycle(loop, bot, provider, monkeypatch) == [
        "ok", "\U0001f4a4 30min 10:30",
    ]


def test_lie_down_notice_is_not_sent_during_the_turn(tmp_path, monkeypatch):
    """_stream_response alone must emit nothing — the receipt waits for the
    end of the reply cycle."""
    import synapse_tg.loop as mod
    _FixedClock(2026, 7, 26, 10, 0).install(monkeypatch, mod)
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_turn_with_tool_use({"next_wake_min": 30})])
    text, _ = _stream(loop, bot, provider, monkeypatch)
    assert text == "ok"
    assert bot.sent == []
    assert loop._pending_notices == ["\U0001f4a4 30min 10:30"]


def test_textless_turn_still_ships_its_receipt(tmp_path, monkeypatch):
    import synapse_tg.loop as mod
    _FixedClock(2026, 7, 26, 10, 0).install(monkeypatch, mod)
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn_with_tool_use({"next_wake_min": 30}, text=""),
    ])
    assert _cycle(loop, bot, provider, monkeypatch) == ["\U0001f4a4 30min 10:30"]


def test_lie_down_rotate_true_sends_no_lie_down_receipt(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn_with_tool_use({"next_wake_min": 30, "rotate": True}),
    ])
    assert _cycle(loop, bot, provider, monkeypatch) == ["ok"]


def test_lie_down_bad_next_wake_min_skips_without_crashing(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_turn_with_tool_use({})])
    assert _cycle(loop, bot, provider, monkeypatch) == ["ok"]


def test_other_tool_use_sends_no_receipt(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([
        _turn_with_tool_use({"path": "x"}, name="Read"),
    ])
    assert _cycle(loop, bot, provider, monkeypatch) == ["ok"]


def test_lie_down_template_override_applies(tmp_path, monkeypatch):
    from synapse_core.commands import messages
    import synapse_tg.loop as mod
    _FixedClock(2026, 7, 26, 10, 0).install(monkeypatch, mod)
    messages.load_overrides({"shell.lie_down": {"cn": "睡 {min} 分钟，{time} 见"}})
    try:
        loop = _loop(tmp_path)
        bot = FakeBot()
        provider = ScriptedProvider([_turn_with_tool_use({"next_wake_min": 30})])
        assert _cycle(loop, bot, provider, monkeypatch) == [
            "ok", "睡 30 分钟，10:30 见",
        ]
    finally:
        messages.load_overrides({})


# ── 🔄 transfer receipt ───────────────────────────────────────────────────────

def _transfer_turn(tool_input, *, text="ok"):
    return _turn_with_tool_use(tool_input, name="mcp__marrow__transfer", text=text)


def test_transfer_receipt_names_the_peer_shell(tmp_path, monkeypatch):
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_transfer_turn({})])
    assert _cycle(loop, bot, provider, monkeypatch) == [
        "ok", "\U0001f504 Transferred to cli",
    ]


def test_transfer_peer_falls_back_when_shells_names_no_other(tmp_path, monkeypatch):
    loop = _loop(tmp_path, shells=("tg",))
    bot = FakeBot()
    provider = ScriptedProvider([_transfer_turn({})])
    assert _cycle(loop, bot, provider, monkeypatch)[-1] == "\U0001f504 Transferred to ?"


def test_transfer_rotate_emits_one_combined_receipt(tmp_path, monkeypatch, no_spawn, real_sleep):
    """One action, one receipt: transfer(rotate=True) owns its rotation, so
    the rotate path adds no 🌙 of its own."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_transfer_turn({"rotate": True})])
    loop.attach_bot(bot)
    ctx = _arm_cycle(loop, bot, provider, monkeypatch)

    async def run():
        task = asyncio.create_task(loop.check_flush(ctx))
        await asyncio.sleep(0)            # let the turn take the lock
        await loop.shell_rotate(None)     # the duty rotation the transfer kicked
        await task

    asyncio.run(run())
    assert [m["text"] for m in bot.sent] == [
        "ok", "\U0001f504 Transferred to cli · rotated",
    ]


def test_transfer_template_override_applies(tmp_path, monkeypatch):
    from synapse_core.commands import messages
    messages.load_overrides({"shell.transferred": {"cn": "交班给{shell}啦"}})
    try:
        loop = _loop(tmp_path)
        bot = FakeBot()
        provider = ScriptedProvider([_transfer_turn({})])
        assert _cycle(loop, bot, provider, monkeypatch) == ["ok", "交班给cli啦"]
    finally:
        messages.load_overrides({})


# ── 🌙 rotate receipt: deferred behind the reply text ─────────────────────────

def test_rotate_notice_lands_under_the_reply_text(tmp_path, monkeypatch, no_spawn, real_sleep):
    """A rotate kick landing mid-turn used to beat the reply out the door."""
    import synapse_tg.loop as mod
    _FixedClock(2026, 7, 26, 10, 0).install(monkeypatch, mod)
    loop = _loop(tmp_path)
    bot = FakeBot()
    provider = ScriptedProvider([_turn("bye for now")])
    loop.attach_bot(bot)
    ctx = _arm_cycle(loop, bot, provider, monkeypatch)

    async def run():
        task = asyncio.create_task(loop.check_flush(ctx))
        await asyncio.sleep(0)            # let the turn take the lock
        await loop.shell_rotate(None)
        # The rotate won the lock back while the reply was still shipping:
        # its receipt had to be queued, not sent.
        assert not task.done()
        await task

    asyncio.run(run())
    assert [m["text"] for m in bot.sent] == ["bye for now", "\U0001f319 Rotated"]


def test_rotate_notice_ships_at_once_outside_a_reply_cycle(tmp_path, no_spawn):
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop.attach_bot(bot)
    asyncio.run(loop.shell_rotate(None))
    assert [m["text"] for m in bot.sent] == ["\U0001f319 Rotated"]
