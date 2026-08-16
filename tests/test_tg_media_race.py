"""Regression: a media message must join the text bubble sent seconds before
it, instead of becoming its own turn.

Pre-fix the media handlers downloaded the file BEFORE touching the inbound
buffer, so the 0.5s check_flush job could flush the earlier text alone while
the handler was parked on the download; the media then landed in an empty
buffer and ran a second turn. Handlers now reserve the buffer slot
synchronously and the download is deferred to flush time.

Every boundary is mocked: no telegram network, no cc spawn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop

CHAT_ID = 123


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return type("M", (), {"message_id": len(self.messages)})()

    async def send_chat_action(self, **_kwargs):
        return None


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


class RecordingProvider:
    """Captures the body of every turn without spawning anything."""

    session_id = None
    turn_output_capped = False

    def __init__(self) -> None:
        self.bodies: list[str] = []

    def spawn(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def send(self, body: str) -> None:
        self.bodies.append(body)

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeSticker:
    def __init__(self) -> None:
        self.emoji = "🙂"
        self.set_name = "pack"
        self.file_id = "stk-1"


class FakeMessage:
    def __init__(self, *, photo=None, sticker=None, caption=None) -> None:
        self.chat_id = CHAT_ID
        self.message_id = 1
        self.text = None
        self.caption = caption
        self.photo = photo or []
        self.sticker = sticker
        self.animation = None
        self.document = None
        self.video = None
        self.reply_to_message = None


class FakeUpdate:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    clock = FakeClock()
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    loop._buffer = InboundBuffer(clock=clock)
    provider = RecordingProvider()
    loop._provider = provider  # type: ignore[assignment]

    async def _stream(_bot, _chat_id, _typing):
        return "ok", None

    async def _deliver(*_args, **_kwargs):
        return None

    monkeypatch.setattr(loop, "_stream_response", _stream)
    monkeypatch.setattr(loop, "_deliver_reply", _deliver)
    return loop, provider, clock, FakeContext(FakeBot())


def test_text_then_media_stay_in_one_turn(env, tmp_path, monkeypatch) -> None:
    loop, provider, clock, ctx = env
    downloaded = tmp_path / "img.jpg"
    release = asyncio.Event()

    async def slow_photo(_bot, _message, _data_dir):
        await release.wait()
        return [downloaded]

    monkeypatch.setattr("synapse_tg.loop.materialize_photo", slow_photo)

    async def scenario() -> None:
        loop._buffer.add("看这个")
        clock.advance(2.0)
        task = asyncio.create_task(
            loop.on_photo(FakeUpdate(FakeMessage(photo=[object()])), ctx)
        )
        await asyncio.sleep(0)
        # The download is slow: the text's own quiet window expires while it runs.
        clock.advance(4.0)
        await loop.check_flush(ctx)
        release.set()
        await task
        clock.advance(6.0)
        await loop.check_flush(ctx)

    asyncio.run(scenario())

    assert provider.bodies == [f"看这个\nUse the Read tool to view: {downloaded}"]


def test_captionless_sticker_alone_flushes_one_turn(env, tmp_path, monkeypatch) -> None:
    loop, provider, clock, ctx = env
    downloaded = tmp_path / "sticker.webp"

    async def fake_sticker(_bot, _message, _data_dir):
        return downloaded

    monkeypatch.setattr("synapse_tg.loop.materialize_sticker", fake_sticker)

    async def scenario() -> None:
        await loop.on_sticker(FakeUpdate(FakeMessage(sticker=FakeSticker())), ctx)
        assert loop._buffer.ready() is False
        clock.advance(6.0)
        await loop.check_flush(ctx)

    asyncio.run(scenario())

    assert provider.bodies == [
        f"[sticker: emoji=🙂, set=pack]\nUse the Read tool to view: {downloaded}"
    ]


def test_captionless_photo_body_carries_no_placeholder(env, tmp_path, monkeypatch) -> None:
    loop, provider, clock, ctx = env
    downloaded = tmp_path / "img.jpg"

    async def fake_photo(_bot, _message, _data_dir):
        return [downloaded]

    monkeypatch.setattr("synapse_tg.loop.materialize_photo", fake_photo)

    async def scenario() -> None:
        await loop.on_photo(FakeUpdate(FakeMessage(photo=[object()])), ctx)
        clock.advance(6.0)
        await loop.check_flush(ctx)

    asyncio.run(scenario())

    assert provider.bodies == [f"Use the Read tool to view: {downloaded}"]


def test_caption_precedes_read_instruction(env, tmp_path, monkeypatch) -> None:
    loop, provider, clock, ctx = env
    downloaded = tmp_path / "img.jpg"

    async def fake_photo(_bot, _message, _data_dir):
        return [downloaded]

    monkeypatch.setattr("synapse_tg.loop.materialize_photo", fake_photo)

    async def scenario() -> None:
        await loop.on_photo(
            FakeUpdate(FakeMessage(photo=[object()], caption="这是什么")), ctx
        )
        clock.advance(6.0)
        await loop.check_flush(ctx)

    asyncio.run(scenario())

    assert provider.bodies == [f"这是什么\nUse the Read tool to view: {downloaded}"]


def test_download_failure_still_ships_the_turn(env, monkeypatch) -> None:
    loop, provider, clock, ctx = env

    async def boom(_bot, _message, _data_dir):
        raise RuntimeError("telegram said no")

    monkeypatch.setattr("synapse_tg.loop.materialize_photo", boom)

    async def scenario() -> None:
        loop._buffer.add("看这个")
        await loop.on_photo(FakeUpdate(FakeMessage(photo=[object()])), ctx)
        clock.advance(6.0)
        await loop.check_flush(ctx)

    asyncio.run(scenario())

    assert provider.bodies == ["看这个\n[media failed to download]"]
    assert loop._pending_media == []
