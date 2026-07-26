"""Inbound slash commands must not reset the tg shell's idle timer, while
every other _track side effect (bot/chat tracking, watch-reply kick) still
runs. Only the shell's on_user_message() call is gated."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


# ── Registry.is_command ─────────────────────────────────────────


def _noop(*_a, **_k) -> None:
    return None


def _registry() -> Registry:
    return Registry(CommandContext(
        state=BridgeState(),
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
    ))


@pytest.mark.parametrize("text", ["/info", "/Info", "/model sonnet", "/clear",
                                   "/resume 1", "/help"])
def test_is_command_true_for_known_slash_commands(text: str) -> None:
    assert _registry().is_command(text) is True


@pytest.mark.parametrize("text", ["/xyz", "/", "hello", "", "info", "/foo bar"])
def test_is_command_false_for_unknown_or_non_slash_text(text: str) -> None:
    assert _registry().is_command(text) is False


def test_is_command_none_is_false() -> None:
    assert _registry().is_command(None) is False  # type: ignore[arg-type]


# ── TgLoop.on_message idle gating ────────────────────────────────


class _ShellSpy:
    def __init__(self) -> None:
        self.calls = 0

    def on_user_message(self) -> None:
        self.calls += 1


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


async def test_registered_slash_command_does_not_reset_idle(tmp_path: Path, monkeypatch) -> None:
    loop, shell = _build_loop(tmp_path)
    kick_calls: list[str] = []
    monkeypatch.setattr(
        TgLoop, "_inbound_from_her",
        lambda self, text="", msg_date=None, media_type="": kick_calls.append(text),
    )
    update = _FakeUpdate(_FakeMessage("/info"))
    await loop.on_message(update, _FakeCtx(_FakeBot()))

    assert shell.calls == 0
    # Rest of _track still ran: bot/chat tracked, watch-reply kick invoked.
    assert loop._pending_chat_id == 111
    assert kick_calls == ["/info"]
    # Command was actually dispatched (ack sent back).
    assert update.message.replies


async def test_plain_text_resets_idle(tmp_path: Path, monkeypatch) -> None:
    loop, shell = _build_loop(tmp_path)
    monkeypatch.setattr(
        TgLoop, "_inbound_from_her",
        lambda self, text="", msg_date=None, media_type="": None,
    )
    update = _FakeUpdate(_FakeMessage("hello there"))
    await loop.on_message(update, _FakeCtx(_FakeBot()))

    assert shell.calls == 1


async def test_unregistered_slash_text_resets_idle(tmp_path: Path, monkeypatch) -> None:
    """A leading-'/' that is NOT a known command is still "handled" by
    dispatch (unknown.cmd ack) — treat it like any other consumed message."""
    loop, shell = _build_loop(tmp_path)
    monkeypatch.setattr(
        TgLoop, "_inbound_from_her",
        lambda self, text="", msg_date=None, media_type="": None,
    )
    update = _FakeUpdate(_FakeMessage("/xyz"))
    await loop.on_message(update, _FakeCtx(_FakeBot()))

    assert shell.calls == 1
    assert update.message.replies  # unknown.cmd ack still sent


async def test_photo_handler_still_resets_idle(tmp_path: Path, monkeypatch) -> None:
    """Media handlers are untouched by the gating — always count as activity."""
    loop, shell = _build_loop(tmp_path)
    monkeypatch.setattr(
        TgLoop, "_inbound_from_her",
        lambda self, text="", msg_date=None, media_type="": None,
    )

    class _FakeMsg(_FakeMessage):
        def __init__(self) -> None:
            super().__init__("")
            self.photo = [object()]
            self.caption = None

    update = _FakeUpdate(_FakeMsg())

    async def _no_photo(bot, message, data_dir):
        return []

    monkeypatch.setattr("synapse_tg.loop.materialize_photo", _no_photo)
    await loop.on_photo(update, _FakeCtx(_FakeBot()))

    assert shell.calls == 1
