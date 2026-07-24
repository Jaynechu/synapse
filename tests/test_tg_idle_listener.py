"""T3: resident idle listener drains unsolicited turns between sends.

Mock at the provider boundary (poll_line + recv yield dicts); never spawn
claude. Provider death is surfaced as POLL_EOF; the listener never dies from
an exception and re-reads self._provider every iteration.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from synapse_core.providers.cc import POLL_EOF
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.typing = 0

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return type("M", (), {"message_id": len(self.sent)})()

    async def send_chat_action(self, **_):
        self.typing += 1
        return None


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
            yield json.loads(first_line)
            if json.loads(first_line).get("type") == "result":
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


class _StubTyping:
    """No-op typing indicator that records that it ran (running stays True).
    Avoids TypingAction's real re-ping task so tests don't need real sleeps."""

    _instances: list = []

    def __init__(self, bot, chat_id) -> None:
        self.bot = bot
        self.started = False
        _StubTyping._instances.append(self)

    running = True

    def start(self) -> None:
        self.started = True
        # Fire one indicator so tests can assert typing ran.
        if getattr(self.bot, "typing", None) is not None:
            self.bot.typing += 1

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def stub_typing(monkeypatch):
    _StubTyping._instances = []
    monkeypatch.setattr("synapse_tg.loop.TypingAction", _StubTyping)


def _loop(tmp_path, alerts=None):
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg, alerts=alerts)
    monkeypatch_split(loop)
    return loop


def monkeypatch_split(loop):
    import synapse_tg.loop as mod
    mod.split_for_tg_typed = lambda text: [{"kind": "text", "text": text}]
    mod.gfm_to_tg_html = lambda t: t


def test_idle_unsolicited_delivered_without_inbound(tmp_path):
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    loop._provider = QueueProvider(_turn_lines("background answer"))
    asyncio.run(loop._listen_once())
    assert [m["text"] for m in bot.sent] == ["background answer"]
    # typing indicator ran during generation
    assert bot.typing >= 1


class NoRecvProvider(QueueProvider):
    """Provider whose recv must never be entered (blocking drain forbidden)."""

    def recv(self, first_line=None):
        raise AssertionError("recv entered for a non-turn first line")


def test_init_handshake_does_not_start_typing_or_drain(tmp_path):
    """Every spawn (fresh, --resume, shell_respawn) emits system{init} first.
    The listener must consume it without typing and without entering recv
    (which would block idle_hard_s then SIGKILL the fresh process)."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    prov = NoRecvProvider(
        [json.dumps({"type": "system", "subtype": "init", "session_id": "sid-new"})]
    )
    loop._provider = prov
    asyncio.run(loop._listen_once())
    assert bot.typing == 0
    assert bot.sent == []
    assert _StubTyping._instances == []
    assert loop._state.session_id == "sid-new"
    assert prov.alive is True
    # Next iteration keeps working (loop stayed healthy).
    asyncio.run(loop._listen_once())
    assert bot.sent == []


def test_non_turn_first_line_consumed_not_drained(tmp_path):
    """A stray non-task_notification line (or garbage) is consumed, never
    treated as the start of an unsolicited turn."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    loop._provider = NoRecvProvider(
        ["not json at all", json.dumps({"type": "stream_event"})]
    )
    asyncio.run(loop._listen_once())
    asyncio.run(loop._listen_once())
    assert bot.typing == 0
    assert bot.sent == []


def test_handshake_before_unsolicited_turn_still_delivers(tmp_path):
    """A handshake queued ahead of a real unsolicited turn must not eat it."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "sid-new"})
    ] + _turn_lines("bg answer")
    loop._provider = QueueProvider(lines)
    asyncio.run(loop._listen_once())  # consumes the handshake only
    assert bot.sent == []
    asyncio.run(loop._listen_once())  # now the real turn
    assert [m["text"] for m in bot.sent] == ["bg answer"]
    assert bot.typing >= 1


def test_idle_none_poll_is_noop(tmp_path):
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    loop._provider = QueueProvider([])  # poll_line -> None
    asyncio.run(loop._listen_once())
    assert bot.sent == []


def test_poll_eof_marks_dead_no_respawn(tmp_path):
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    prov = QueueProvider([POLL_EOF])
    loop._provider = prov
    asyncio.run(loop._listen_once())
    assert prov.alive is False
    # Listener does NOT respawn; provider object stays (lazy respawn on send).
    assert loop._provider is prov
    assert bot.sent == []


def test_consecutive_back_to_back_turns(tmp_path):
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    lines = _turn_lines("bg one") + _turn_lines("bg two")
    loop._provider = QueueProvider(lines)
    asyncio.run(loop._listen_once())
    assert [m["text"] for m in bot.sent] == ["bg one", "bg two"]


def test_no_chat_target_drops_with_warning_no_crash(tmp_path):
    loop = _loop(tmp_path)
    loop._bot = None
    loop._pending_chat_id = None
    loop._provider = QueueProvider(_turn_lines("orphan"))
    # Must not raise; turn drained, nothing sent (no bot).
    asyncio.run(loop._listen_once())


def test_dead_provider_is_noop(tmp_path):
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    prov = QueueProvider(_turn_lines("x"))
    prov.alive = False
    loop._provider = prov
    asyncio.run(loop._listen_once())
    assert bot.sent == []


def test_none_provider_is_noop(tmp_path):
    loop = _loop(tmp_path)
    loop._bot = FakeBot()
    loop._pending_chat_id = 123
    loop._provider = None
    asyncio.run(loop._listen_once())  # no crash


def test_exception_in_delivery_does_not_kill_listener(tmp_path):
    """A delivery blow-up in one iteration is caught by _idle_listener's
    catch-all; the loop keeps running and exits cleanly on the stop event."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    loop._provider = QueueProvider(_turn_lines("boom"))

    async def bad_deliver(*a, **k):
        raise RuntimeError("delivery blew up")

    loop._deliver_reply = bad_deliver  # type: ignore[assignment]

    async def run_listener():
        # Stop after the first guarded iteration.
        async def stop_soon():
            await asyncio.sleep(0)
            loop.stop_listener()

        asyncio.ensure_future(stop_soon())
        await loop._idle_listener()

    # Must return without the exception escaping.
    asyncio.run(run_listener())


def test_provider_swapped_mid_poll_picked_up_next_iteration(tmp_path):
    """The listener must re-read self._provider inside the lock. Swap the
    provider before the iteration; the new object is used, no crash."""
    loop = _loop(tmp_path)
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 123
    old = QueueProvider([])
    loop._provider = old
    # Simulate a swap: replace with a fresh provider that has a real turn.
    loop._provider = QueueProvider(_turn_lines("after swap"))
    asyncio.run(loop._listen_once())
    assert [m["text"] for m in bot.sent] == ["after swap"]
