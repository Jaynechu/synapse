"""Only a message the registry forwards to the LLM counts as user activity on
tg: anything dispatch swallows (known command, typo slash, bare alias, picker
digit) must leave the shell's silence window and any booked wake untouched,
while the rest of _track (bot/chat tracking, watch-reply kick) still runs.

Every boundary is mocked: no cc spawn, no telegram bot, no real clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from synapse_core import shell_state
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_tg.shell import ShellHost, parse_wake_at

MIN = 60.0


class _NoSpawnProvider:
    """Stands in for ClaudeCodeProvider at the spawn boundary: an alias swap
    ("opus") runs the real _swap_provider path without starting a cc process."""

    session_id = None
    turn_output_capped = False

    def __init__(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def is_alive(self) -> bool:
        return True

    def spawn(self) -> None:
        pass

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch):
    monkeypatch.setattr("synapse_tg.loop.ClaudeCodeProvider", _NoSpawnProvider)


@pytest.fixture(autouse=True)
def stub_kick(monkeypatch):
    """The watch-reply kick shells out; tests that assert on it re-patch."""
    monkeypatch.setattr(
        TgLoop, "_inbound_from_her",
        lambda self, text="", msg_date=None, media_type="": None,
    )


class _ShellSpy:
    def __init__(self) -> None:
        self.calls = 0

    def on_user_message(self) -> None:
        self.calls += 1

    def fold_session(self, _sid: str | None = None) -> None:
        pass


class _FakeMessage:
    def __init__(self, text: str, chat_id: int = 111, message_id: int = 1) -> None:
        self.text = text
        self.chat_id = chat_id
        self.date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.message_id = message_id
        self.reply_to_message = None
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _FakeCtx:
    def __init__(self, bot: _FakeBot) -> None:
        self.bot = bot


def _build_loop(tmp_path: Path, chat_id: int = 111) -> tuple[TgLoop, _ShellSpy]:
    cfg = TgConfig(data_dir=tmp_path / "tg-data", chat_id=chat_id, marrow_db="")
    loop = TgLoop(cfg)
    shell = _ShellSpy()
    loop.attach_shell(shell)
    return loop, shell


# ── on_message: which inputs count as activity ───────────────────


@pytest.mark.parametrize("text", [
    "/info",                # known command
    "/model",               # known command, no argument
    "/xyz",                 # typo slash -> unknown.cmd ack
    "/opus 5o",             # typo of a model switch
    "/ct-wake",             # skill name, never a bridge command
    "opus",                 # bare natural alias
])
async def test_dispatch_handled_text_does_not_count_as_activity(
    tmp_path: Path, text: str
) -> None:
    loop, shell = _build_loop(tmp_path)
    update = _FakeUpdate(_FakeMessage(text))
    await loop.on_message(update, _FakeCtx(_FakeBot()))

    assert shell.calls == 0
    assert update.message.replies          # something was acked back
    # Rest of _track still ran: the outbound chat is tracked either way.
    assert loop._pending_chat_id == 111


@pytest.mark.parametrize("text", ["hello there", "info", "model opus"])
async def test_forwarded_text_counts_as_activity(tmp_path: Path, text: str) -> None:
    loop, shell = _build_loop(tmp_path)
    update = _FakeUpdate(_FakeMessage(text))
    await loop.on_message(update, _FakeCtx(_FakeBot()))

    assert shell.calls == 1


async def test_watch_reply_kick_still_fires_for_a_command(tmp_path: Path, monkeypatch) -> None:
    loop, shell = _build_loop(tmp_path)
    kicks: list[str] = []
    monkeypatch.setattr(
        TgLoop, "_inbound_from_her",
        lambda self, text="", msg_date=None, media_type="": kicks.append(text),
    )
    await loop.on_message(_FakeUpdate(_FakeMessage("/info")), _FakeCtx(_FakeBot()))

    assert shell.calls == 0
    assert kicks == ["/info"]


async def test_photo_handler_still_resets_idle(tmp_path: Path, monkeypatch) -> None:
    """Media handlers never go through dispatch — always count as activity."""
    loop, shell = _build_loop(tmp_path)

    class _FakeMsg(_FakeMessage):
        def __init__(self) -> None:
            super().__init__("")
            self.photo = [object()]
            self.caption = None

    async def _no_photo(bot, message, data_dir):
        return []

    monkeypatch.setattr("synapse_tg.loop.materialize_photo", _no_photo)
    await loop.on_photo(_FakeUpdate(_FakeMsg()), _FakeCtx(_FakeBot()))

    assert shell.calls == 1


# ── end to end: the booked wake in the shell ledger ──────────────


class _Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _booked_loop(tmp_path: Path) -> tuple[TgLoop, _Clock]:
    """A real ShellHost over a real TgLoop, asleep until +3h."""
    clock = _Clock()
    cfg = TgConfig(
        data_dir=tmp_path / "tg-data",
        chat_id=111,
        marrow_db=str(tmp_path / "marrow.db"),
        shell_state_dir=str(tmp_path / "shells"),
        shell_socket=str(tmp_path / "s.sock"),
        shell_idle_min=20.0,
    )
    loop = TgLoop(cfg)
    shell_state.write(tmp_path / "shells", "tg",
                      {"next_wake_at": _iso_utc(clock.t + 180 * MIN)})
    host = ShellHost(cfg, loop, clock=clock)
    loop.attach_shell(host)
    return loop, clock


def _wake(tmp_path: Path) -> float | None:
    return parse_wake_at(shell_state.read(tmp_path / "shells", "tg").get("next_wake_at"))


@pytest.mark.parametrize("text", ["/opus 5o", "/model", "opus", "/ct-wake"])
def test_handled_text_keeps_the_booked_wake(tmp_path: Path, text: str) -> None:
    loop, clock = _booked_loop(tmp_path)
    booked = _wake(tmp_path)

    asyncio.run(loop.on_message(_FakeUpdate(_FakeMessage(text)), _FakeCtx(_FakeBot())))

    assert _wake(tmp_path) == booked


def test_plain_text_cancels_the_booked_wake(tmp_path: Path) -> None:
    loop, clock = _booked_loop(tmp_path)

    asyncio.run(loop.on_message(_FakeUpdate(_FakeMessage("还没睡")), _FakeCtx(_FakeBot())))

    assert _wake(tmp_path) is None
    st = shell_state.read(tmp_path / "shells", "tg")
    assert parse_wake_at(st.get("last_user_ts")) == pytest.approx(clock.t, abs=1)
