"""B7: consecutive provider-death counter + escalation alert at >=3."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.alerts import AlertSink
from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.mock import EchoProvider
from synapse_core.providers.errors import ProviderDeadError
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
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def poll_messages(self) -> list:
        return []

    def send_text(self, to, ctx, text, **_kw) -> bool:
        self.sent.append((to, ctx, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


@pytest.fixture()
def env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    return {"state": state, "sessions": sessions, "tmp": tmp_path}


def _make_loop(env, ilink, alerts=None):
    clock = FakeClock()
    return MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 15, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
        alerts=alerts,
    ), clock


def test_death_count_resets_on_successful_recv(env) -> None:
    """Counter resets to 0 after a successful _drain_recv."""
    ilink = FakeILink()
    loop, _ = _make_loop(env, ilink)
    loop._provider_death_count = 2

    provider = EchoProvider()
    provider.spawn()
    loop._provider = provider
    loop._last_from_wxid = "testuser"
    loop._last_ctx_token = "ctx"

    # Trigger a real drain — EchoProvider returns a result event.
    loop._drain_recv()

    assert loop._provider_death_count == 0


def test_no_alert_on_first_death_with_sid(env, tmp_path) -> None:
    """First death with session_id set → counter=1, NO alert."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    ilink = FakeILink()
    loop, _ = _make_loop(env, ilink, alerts=alerts)
    env["state"].session_id = "sid-alive"
    loop._last_from_wxid = "testuser"
    loop._last_ctx_token = "ctx"

    loop._handle_provider_dead(RuntimeError("boom"), "testuser", "ctx")

    assert loop._provider_death_count == 1
    alert_files = list((tmp_path / "alerts").glob("*.txt"))
    assert len(alert_files) == 0
    assert len(ilink.sent) == 0


def test_no_alert_on_second_death_with_sid(env, tmp_path) -> None:
    """Second death → counter=2, still no alert."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    ilink = FakeILink()
    loop, _ = _make_loop(env, ilink, alerts=alerts)
    env["state"].session_id = "sid-alive"
    loop._provider_death_count = 1

    loop._handle_provider_dead(RuntimeError("boom"), "testuser", "ctx")

    assert loop._provider_death_count == 2
    assert len(list((tmp_path / "alerts").glob("*.txt"))) == 0


def test_alert_and_bubble_on_third_death_with_sid(env, tmp_path) -> None:
    """Third consecutive death with sid set → critical alert + user bubble."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    ilink = FakeILink()
    loop, _ = _make_loop(env, ilink, alerts=alerts)
    env["state"].session_id = "sid-alive"
    loop._provider_death_count = 2

    loop._handle_provider_dead(RuntimeError("cc died"), "testuser", "ctx")

    assert loop._provider_death_count == 3
    alert_files = list((tmp_path / "alerts").glob("*.txt"))
    assert len(alert_files) == 1
    import json
    data = json.loads(alert_files[0].read_text())
    assert data["severity"] == "critical"
    assert data["fingerprint"] == "provider.dead"
    assert "consecutive" in data["message"]
    # User bubble sent
    assert len(ilink.sent) == 1
    assert ilink.sent[0][0] == "testuser"


def test_alert_on_death_without_sid(env, tmp_path) -> None:
    """Death with no session_id → immediate critical alert (original behaviour)."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    ilink = FakeILink()
    loop, _ = _make_loop(env, ilink, alerts=alerts)
    env["state"].session_id = None

    loop._handle_provider_dead(RuntimeError("gone"), "testuser", "ctx")

    alert_files = list((tmp_path / "alerts").glob("*.txt"))
    assert len(alert_files) == 1
    import json
    data = json.loads(alert_files[0].read_text())
    assert data["severity"] == "critical"
    assert data["fingerprint"] == "provider.dead"
    assert len(ilink.sent) == 1


def test_ensure_provider_spawn_failure_routes_to_counter(env, tmp_path) -> None:
    """_ensure_provider spawn failure increments death counter."""
    alerts = AlertSink(alerts_dir=tmp_path / "alerts")
    ilink = FakeILink()
    loop, _ = _make_loop(env, ilink, alerts=alerts)
    env["state"].session_id = "sid-alive"
    loop._last_from_wxid = "testuser"
    loop._last_ctx_token = "ctx"

    def bad_factory(model=None, resume_sid=None):
        p = EchoProvider()
        # spawn() will raise
        p.spawn = lambda **_kw: (_ for _ in ()).throw(OSError("spawn failed"))
        return p

    loop._provider_factory = bad_factory
    loop._provider = None  # ensure dead

    result = loop._ensure_provider()

    assert result is False
    assert loop._provider_death_count >= 1
