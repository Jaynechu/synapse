"""B11: on next inbound after idle close, MainLoop lazy-spawns provider via
factory(model=state.model, resume_sid=state.session_id).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


@pytest.fixture()
def env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    return {"state": state, "sessions": sessions, "tmp": tmp_path}


def _make_loop(env, factory, clock, buffer=None):
    return MainLoop(
        ilink=object(),
        provider_factory=factory,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=buffer or InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
    )


def test_maybe_flush_lazy_spawns_dead_provider_with_resume_sid(env) -> None:
    """state.session_id set + provider dead → factory(resume_sid=sid, model=state.model)."""
    clock = FakeClock()
    env["state"].session_id = "sid-idle-keep"
    env["state"].model = "opus-4.6[1m]"

    factory_calls: list[dict] = []

    def factory(model=None, resume_sid=None):
        factory_calls.append({"model": model, "resume_sid": resume_sid})
        p = EchoProvider()
        p.spawn()
        return p

    buffer = InboundBuffer(clock=clock)
    loop = _make_loop(env, factory, clock, buffer=buffer)
    # No provider yet (simulating post-idle state where provider was closed).
    assert loop._provider is None

    # Add a buffered message and trip the quiet-window.
    loop._buffer.add("ping")
    loop._last_from_wxid = "testuser"
    loop._last_ctx_token = "ctx"
    clock.advance(10.0)

    loop.maybe_flush()

    assert len(factory_calls) == 1
    assert factory_calls[0]["resume_sid"] == "sid-idle-keep"
    assert factory_calls[0]["model"] == "opus-4.6[1m]"
    assert loop._provider is not None
    assert loop._provider.is_alive()


def test_maybe_flush_no_lazy_spawn_without_sid(env) -> None:
    """No state.session_id → do not lazy-spawn (no continuation point)."""
    clock = FakeClock()
    env["state"].session_id = None

    factory_calls: list[dict] = []

    def factory(model=None, resume_sid=None):
        factory_calls.append({"model": model, "resume_sid": resume_sid})
        p = EchoProvider()
        p.spawn()
        return p

    loop = _make_loop(env, factory, clock)
    assert loop._provider is None

    loop._buffer.add("hi")
    loop._last_from_wxid = "testuser"
    loop._last_ctx_token = "ctx"
    clock.advance(10.0)

    loop.maybe_flush()
    # No factory call — bail early when no sid to resume.
    assert factory_calls == []


def test_maybe_flush_reuses_live_provider(env) -> None:
    """Existing live provider → no factory call, normal flush."""
    clock = FakeClock()
    env["state"].session_id = "sid-old"

    factory_calls: list[dict] = []

    def factory(model=None, resume_sid=None):
        factory_calls.append({"model": model, "resume_sid": resume_sid})
        p = EchoProvider()
        p.spawn()
        return p

    loop = _make_loop(env, factory, clock)
    # Pre-spawn a live provider; lazy path should NOT trigger.
    live = EchoProvider()
    live.spawn()
    loop._provider = live

    loop._buffer.add("hi")
    loop._last_from_wxid = "testuser"
    loop._last_ctx_token = "ctx"
    clock.advance(10.0)

    loop.maybe_flush()
    assert factory_calls == []
    assert loop._provider is live


def test_maybe_flush_lazy_spawn_when_provider_dead_object(env) -> None:
    """Provider object exists but is_alive()==False → respawn via factory."""
    clock = FakeClock()
    env["state"].session_id = "sid-dead-obj"
    env["state"].model = None  # No explicit model

    factory_calls: list[dict] = []

    def factory(model=None, resume_sid=None):
        factory_calls.append({"model": model, "resume_sid": resume_sid})
        p = EchoProvider()
        p.spawn()
        return p

    loop = _make_loop(env, factory, clock)
    dead = EchoProvider()
    dead.spawn()
    dead.close()  # alive=False now
    loop._provider = dead

    loop._buffer.add("hi")
    loop._last_from_wxid = "testuser"
    loop._last_ctx_token = "ctx"
    clock.advance(10.0)

    loop.maybe_flush()
    assert len(factory_calls) == 1
    assert factory_calls[0]["resume_sid"] == "sid-dead-obj"
    assert loop._provider is not dead
    assert loop._provider.is_alive()
