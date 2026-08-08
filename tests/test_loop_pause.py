"""Tests for MainLoop pause/resume + AlertSink wiring on ProviderDeadError."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from synapse_core.alerts import AlertSink
from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.base import Provider
from synapse_core.providers.errors import ProviderDeadError
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


class FakeILink:
    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches)
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        return self._batches.pop(0) if self._batches else []

    def send_text(
        self, to_user_id: str, ctx_token: str, text: str, **_kwargs
    ) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class DeadProvider(Provider):
    """Provider that spawns fine, accepts send, then raises ProviderDeadError on recv."""

    def __init__(self) -> None:
        self.alive = False
        self.usage_total: dict[str, int] = {}
        self.session_id: str | None = None

    def spawn(self, env: dict[str, str] | None = None) -> None:
        self.alive = True

    def send(self, msg: str) -> None:
        pass

    def recv(self) -> Iterator[dict[str, Any]]:
        raise ProviderDeadError("subprocess died during recv")
        yield  # pragma: no cover - make this a generator

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


@pytest.fixture()
def env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    return {"state": state, "sessions": sessions, "tmp": tmp_path}


def _build_loop(
    env, ilink, clock, wallclock, provider_cls, alerts: AlertSink | None
) -> MainLoop:
    return MainLoop(
        ilink=ilink,
        provider_factory=provider_cls,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=wallclock,
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts-stub",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
        alerts=alerts,
    )


def test_pause_blocks_tick_and_flush(env) -> None:
    inbound = [[{"from_wxid": "testuser", "context_token": "ctx", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock()
    loop = _build_loop(
        env, ilink, clock, lambda: datetime(2026, 6, 2, 12, 0), EchoProvider, None
    )
    loop._provider = EchoProvider()
    loop._provider.spawn()

    loop.pause_poll()
    # Drive the internal loop body the way _run does it.
    if not loop._paused:
        loop.tick()
        loop.maybe_flush()
    assert ilink.sent == []
    assert len(loop._buffer) == 0

    loop.resume_poll()
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert ilink.sent  # outbound landed after resume


def test_provider_alive_helper_returns_false_when_no_provider(env) -> None:
    ilink = FakeILink([])
    clock = FakeClock()
    loop = _build_loop(
        env, ilink, clock, lambda: datetime(2026, 6, 2, 12, 0), EchoProvider, None
    )
    assert loop._provider_alive() is False


def test_provider_alive_helper_true_when_spawned(env) -> None:
    ilink = FakeILink([])
    clock = FakeClock()
    loop = _build_loop(
        env, ilink, clock, lambda: datetime(2026, 6, 2, 12, 0), EchoProvider, None
    )
    loop._provider = EchoProvider()
    loop._provider.spawn()
    assert loop._provider_alive() is True
    loop._provider.close()
    assert loop._provider_alive() is False


def test_provider_dead_writes_critical_alert_via_sink(env, tmp_path: Path) -> None:
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    inbound = [[{"from_wxid": "testuser", "context_token": "ctx", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock()
    loop = _build_loop(
        env,
        ilink,
        clock,
        lambda: datetime(2026, 6, 2, 12, 0),
        DeadProvider,
        alerts,
    )
    loop._provider = DeadProvider()
    loop._provider.spawn()

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    rows = alerts.list_recent()
    assert len(rows) == 1
    assert rows[0]["severity"] == "critical"
    assert rows[0]["kind"] == "provider_dead"
    assert rows[0]["source"] == "loop._drain_recv"

    # And the fallback bubble lands.
    assert any("老公已死" in t for (_, _, t) in ilink.sent)


def test_provider_dead_suppressed_when_session_alive(env, tmp_path: Path) -> None:
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    inbound = [[{"from_wxid": "testuser", "context_token": "ctx", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock()
    loop = _build_loop(
        env,
        ilink,
        clock,
        lambda: datetime(2026, 6, 2, 12, 0),
        DeadProvider,
        alerts,
    )
    loop._provider = DeadProvider()
    loop._provider.spawn()
    env["state"].session_id = "alive-sid-1234"

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert alerts.list_recent() == [], "alert should be suppressed when session alive"
    assert not any("老公已死" in t for (_, _, t) in ilink.sent), (
        "dead bubble should be suppressed when session alive"
    )


def test_provider_dead_legacy_alert_stub_when_no_sink(env) -> None:
    """When alerts=None, the legacy alert_dir stub still fires (back-compat)."""
    inbound = [[{"from_wxid": "testuser", "context_token": "ctx", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock()
    loop = _build_loop(
        env,
        ilink,
        clock,
        lambda: datetime(2026, 6, 2, 12, 0),
        DeadProvider,
        None,
    )
    loop._provider = DeadProvider()
    loop._provider.spawn()

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    alert_dir = env["tmp"] / "alerts-stub"
    assert alert_dir.is_dir()
    files = list(alert_dir.iterdir())
    assert files, "legacy stub alert should have been written"
