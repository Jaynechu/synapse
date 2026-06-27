from __future__ import annotations

import asyncio
from pathlib import Path

from synapse_core.debounce import InboundBuffer
from synapse_core.providers.errors import ProviderDeadError
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeBot:
    def __init__(self, *, fail_messages: bool = False) -> None:
        self.fail_messages = fail_messages
        self.messages: list[dict] = []

    async def send_message(self, **kwargs):
        if self.fail_messages:
            raise RuntimeError("rate limited")
        self.messages.append(kwargs)
        return type("SentMessage", (), {"message_id": len(self.messages)})()

    async def send_chat_action(self, **_kwargs) -> None:
        return None


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


class DeadOnSendProvider:
    session_id = None

    def __init__(self) -> None:
        self.alive = True
        self.killed = False
        self.spawned = False

    def spawn(self) -> None:
        self.spawned = True
        self.alive = True

    def send(self, _body: str) -> None:
        raise ProviderDeadError("subprocess not alive")

    def is_alive(self) -> bool:
        return self.alive

    def kill(self) -> None:
        self.killed = True
        self.alive = False


class SpawnedProvider:
    session_id = None

    def __init__(self) -> None:
        self.spawned = False

    def spawn(self) -> None:
        self.spawned = True

    def is_alive(self) -> bool:
        return self.spawned


def _loop_with_body(tmp_path: Path, clock: FakeClock, body: str = "hello") -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    loop = TgLoop(cfg)
    loop._buffer = InboundBuffer(clock=clock)
    loop._buffer.add(body)
    clock.advance(6.0)
    loop._pending_chat_id = 123
    return loop


def test_tg_provider_gives_up_after_max_deaths_without_requeue(tmp_path: Path) -> None:
    clock = FakeClock()
    loop = _loop_with_body(tmp_path, clock)
    loop._death_count = 2
    loop._provider = DeadOnSendProvider()  # type: ignore[assignment]
    bot = FakeBot()

    asyncio.run(loop.check_flush(FakeContext(bot)))  # type: ignore[arg-type]

    assert loop._death_count == 3
    assert loop._provider is None
    assert len(loop._buffer) == 0
    assert [msg["text"] for msg in bot.messages] == ["provider连续暴毙，先停手"]


def test_tg_provider_restart_notice_failure_does_not_crash(tmp_path: Path) -> None:
    clock = FakeClock()
    loop = _loop_with_body(tmp_path, clock)
    old_provider = DeadOnSendProvider()
    new_provider = SpawnedProvider()
    loop._provider = old_provider  # type: ignore[assignment]
    loop._make_provider = lambda: new_provider  # type: ignore[method-assign]
    bot = FakeBot(fail_messages=True)

    asyncio.run(loop.check_flush(FakeContext(bot)))  # type: ignore[arg-type]

    assert loop._death_count == 1
    assert old_provider.killed
    assert loop._provider is new_provider
    assert new_provider.spawned
    assert loop._buffer.flush() == "hello"
