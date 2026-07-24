"""Scheduler timing-engine tests — mocked clock, no real sleeps."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from synapse_core.scheduler import Scheduler, send_kick


class FakeClock:
    """Mocked wall clock; `sleep` advances it by the requested timeout."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, timeout: float) -> None:
        self.sleeps.append(timeout)
        self.now += timeout


def _record(hits: list[str]):
    async def cb(shell: str) -> None:
        hits.append(shell)

    return cb


async def test_exact_time_fire():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=10.0, callback=_record(hits))

    await sched._tick()  # now=0: not due, sleeps to nearest (10)
    assert hits == []
    assert clock.now == 10.0

    await sched._tick()  # now=10: fires
    assert hits == ["cli"]


async def test_sleep_capped_at_safety_tick():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep, safety_tick=60.0)
    sched.schedule("cli", at=10_000.0, callback=_record([]))

    await sched._tick()
    assert clock.sleeps == [60.0]  # capped, not 10000

    await sched._tick()  # still not due after one tick -> caps again
    assert clock.sleeps == [60.0, 60.0]


async def test_kick_preemption():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=1000.0, callback=_record(hits))

    sched.kick("cli")
    await sched._tick()  # kicked -> fires now, deadline ignored
    assert hits == ["cli"]
    assert clock.now == 0.0  # never slept


async def test_kick_unknown_shell_noop():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    sched.kick("ghost")
    await sched._tick()  # nothing scheduled -> sleeps the safety tick
    assert clock.sleeps == [60.0]


async def test_cancel():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=10.0, callback=_record(hits))
    sched.cancel("cli")

    await sched._tick()  # empty table -> safety tick, never fires
    assert hits == []
    clock.now = 100.0
    await sched._tick()
    assert hits == []


async def test_cancel_clears_pending_kick():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=10.0, callback=_record(hits))
    sched.kick("cli")
    sched.cancel("cli")
    await sched._tick()
    assert hits == []


async def test_two_shell_independence():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=5.0, callback=_record(hits))
    sched.schedule("tg", at=20.0, callback=_record(hits))

    await sched._tick()  # sleeps to nearest = 5 (cli)
    assert clock.now == 5.0
    await sched._tick()  # cli fires
    assert hits == ["cli"]

    await sched._tick()  # tg still pending -> sleep 15 to 20
    assert clock.now == 20.0
    await sched._tick()  # tg fires
    assert hits == ["cli", "tg"]


async def test_reschedule_replaces_deadline():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=100.0, callback=_record(hits))
    sched.schedule("cli", at=3.0, callback=_record(hits))  # replace, sooner

    await sched._tick()
    assert clock.now == 3.0
    await sched._tick()
    assert hits == ["cli"]


async def test_past_due_fires_immediately():
    clock = FakeClock()
    clock.now = 50.0
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=10.0, callback=_record(hits))  # already past

    await sched._tick()
    assert hits == ["cli"]
    assert clock.now == 50.0  # no sleep


async def test_callback_exception_does_not_kill_loop():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)

    async def boom(shell: str) -> None:
        raise RuntimeError("callback broke")

    hits: list[str] = []
    sched.schedule("cli", at=0.0, callback=boom)
    sched.schedule("tg", at=0.0, callback=_record(hits))

    await sched._tick()  # both due; cli raises but tg still fires
    assert hits == ["tg"]


async def test_sync_callback_supported():
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    hits: list[str] = []
    sched.schedule("cli", at=0.0, callback=lambda s: hits.append(s))
    await sched._tick()
    assert hits == ["cli"]


@pytest.fixture
def short_sock():
    """Short unix-socket path — macOS AF_UNIX caps sun_path at 104 bytes,
    so pytest's tmp_path is too long."""
    d = Path(tempfile.mkdtemp(prefix="sc", dir="/tmp"))
    p = d / "s.sock"
    yield p
    for f in (p, d):
        try:
            if f.exists():
                f.unlink() if f.is_file() else f.rmdir()
        except OSError:
            pass


async def test_socket_kick_interrupts_real_sleep(short_sock):
    """External kick over the unix socket wakes a real (default) sleep."""
    sock = short_sock
    sched = Scheduler(socket_path=sock)
    hits: list[str] = []
    fired = asyncio.Event()

    async def cb(shell: str) -> None:
        hits.append(shell)
        fired.set()

    sched.schedule("cli", at=1e12, callback=cb)  # far future: only a kick fires it
    task = asyncio.create_task(sched.run())
    await asyncio.sleep(0.05)  # let socket open + loop enter sleep

    await send_kick(sock, "cli")
    await asyncio.wait_for(fired.wait(), timeout=2.0)
    assert hits == ["cli"]

    sched.stop()
    await asyncio.wait_for(task, timeout=2.0)
    assert not sock.exists()  # socket cleaned up on stop


async def test_socket_absent_by_default(tmp_path):
    sched = Scheduler()  # no socket_path
    assert sched._socket_path is None
    task = asyncio.create_task(sched.run())
    await asyncio.sleep(0)
    sched.stop()
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.parametrize("timeout", [0.0, 10.0, 59.9])
async def test_time_to_nearest_clamped(timeout):
    clock = FakeClock()
    sched = Scheduler(clock=clock.time, sleep=clock.sleep)
    sched.schedule("cli", at=timeout, callback=_record([]))
    assert sched._time_to_nearest(0.0) == pytest.approx(min(timeout, 60.0))
