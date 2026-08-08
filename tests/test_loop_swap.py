"""MainLoop swap_provider / forget_session hooks for the command registry (A5)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class TrackingProvider(EchoProvider):
    """EchoProvider variant that records the args its factory passed in."""

    def __init__(
        self, model: str | None = None, resume_sid: str | None = None
    ) -> None:
        super().__init__()
        self.model = model
        self.resume_sid = resume_sid
        self.close_count = 0
        self.cancel_count = 0
        self.spawn_count = 0

    def spawn(self, env: dict[str, str] | None = None) -> None:
        self.spawn_count += 1
        super().spawn(env)

    def close(self) -> None:
        self.close_count += 1
        super().close()

    def cancel(self) -> None:
        self.cancel_count += 1
        super().cancel()


class TrackingFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None]] = []
        self.created: list[TrackingProvider] = []

    def __call__(
        self, model: str | None = None, resume_sid: str | None = None
    ) -> TrackingProvider:
        self.calls.append((model, resume_sid))
        p = TrackingProvider(model=model, resume_sid=resume_sid)
        self.created.append(p)
        return p


@pytest.fixture()
def loop(tmp_path: Path):
    factory = TrackingFactory()
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    main_loop = MainLoop(
        ilink=object(),
        provider_factory=factory,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=lambda: 0.0),
        poll_interval_sec=0.01,
        clock=lambda: 0.0,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    # Prime an initial provider (start() is normally what does this).
    initial = factory()  # no kwargs == initial spawn
    initial.spawn()
    main_loop._provider = initial
    state.usage_total["input_tokens"] = 42  # something to verify reset
    return main_loop, factory, state, sessions


def test_swap_provider_closes_old_spawns_new(loop) -> None:
    main_loop, factory, state, _ = loop
    old = main_loop._provider
    main_loop.swap_provider("claude-opus-4-8", "sid-abc")
    new = main_loop._provider
    assert new is not old
    # B4 fix: swap uses cancel() (≤2s) not close() (≤6s) so the old cc
    # subprocess stops flushing stdout immediately after /stop ack.
    assert old.cancel_count == 1
    assert old.close_count == 0
    assert factory.calls[-1] == ("claude-opus-4-8", "sid-abc")
    assert new.model == "claude-opus-4-8"
    assert new.resume_sid == "sid-abc"
    assert new.spawn_count == 1
    # usage counter resets on swap.
    assert state.usage_total == {}


def test_swap_provider_with_none_resume(loop) -> None:
    main_loop, factory, _, _ = loop
    main_loop.swap_provider("claude-sonnet-4-6", None)
    assert factory.calls[-1] == ("claude-sonnet-4-6", None)


def test_forget_session_drops_current_user(loop) -> None:
    main_loop, _, _, sessions = loop
    sessions.set("testuser", "sid-1")
    main_loop._last_from_wxid = "testuser"
    assert sessions.get("testuser") == "sid-1"
    main_loop.forget_session()
    assert sessions.get("testuser") is None


def test_forget_session_no_user_is_noop(loop) -> None:
    main_loop, _, _, sessions = loop
    sessions.set("other", "sid-keep")
    main_loop._last_from_wxid = None
    main_loop.forget_session()
    assert sessions.get("other") == "sid-keep"


def test_close_provider_no_respawn(loop) -> None:
    main_loop, _, _, _ = loop
    old = main_loop._provider
    main_loop.close_provider()
    assert old.close_count == 1
    assert main_loop._provider is None
