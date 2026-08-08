"""B6 — loop writes ~/.config/marrow/last_active.json after each turn.

The bridge persists `{sid, channel, ts}` per successful turn so cross-channel
`/resume` can show the most-recent session even when marrow `sessions` table
is offline / behind.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from synapse_core import last_active
from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class _ILink:
    def __init__(self, batches: list[list[dict]]) -> None:
        self._batches = list(batches)
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        return self._batches.pop(0) if self._batches else []

    def send_text(self, to: str, ctx: str, text: str) -> bool:
        self.sent.append((to, ctx, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


@pytest.fixture(autouse=True)
def _last_active_path(tmp_path: Path) -> Path:
    return tmp_path / "last_active.json"


def _build_loop(
    state, sessions, ilink, clock, tmp_path: Path, last_active_path: Path
) -> MainLoop:
    return MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        channel_label="CC-WX",
        last_active_path=last_active_path,
    )


def test_successful_turn_writes_last_active(_last_active_path: Path, tmp_path: Path) -> None:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    ilink = _ILink([
        [{"from_wxid": "testuser", "context_token": "ctx", "text": "hello"}],
    ])
    clock = _Clock(start=1000.0)
    loop = _build_loop(state, sessions, ilink, clock, tmp_path, _last_active_path)
    loop._provider = EchoProvider()
    loop._provider.spawn()

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    # Echo provider populated state.session_id.
    assert state.session_id == "mock-sid-0001"
    got = last_active.read(_last_active_path)
    assert got is not None
    assert got["sid"] == "mock-sid-0001"
    assert got["channel"] == "wx"
    assert isinstance(got["ts"], float)


def test_unflushed_buffer_does_not_write(_last_active_path: Path, tmp_path: Path) -> None:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    ilink = _ILink([
        [{"from_wxid": "testuser", "context_token": "ctx", "text": "hi"}],
    ])
    clock = _Clock(start=1000.0)
    loop = _build_loop(state, sessions, ilink, clock, tmp_path, _last_active_path)
    loop._provider = EchoProvider()
    loop._provider.spawn()

    loop.tick()  # buffered, no flush yet
    clock.advance(1.0)
    loop.maybe_flush()  # under quiet window
    assert not _last_active_path.exists()


def test_channel_override_via_constructor(_last_active_path: Path, tmp_path: Path) -> None:
    """Loop accepts `channel="cli"` so the same code can run from the cli."""
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    ilink = _ILink([
        [{"from_wxid": "testuser", "context_token": "ctx", "text": "hi"}],
    ])
    clock = _Clock(start=1000.0)
    loop = MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="cli",
        last_active_path=_last_active_path,
        channel_label="CC-WX",
    )
    loop._provider = EchoProvider()
    loop._provider.spawn()

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    payload = json.loads(_last_active_path.read_text())
    assert payload["channel"] == "cli"
