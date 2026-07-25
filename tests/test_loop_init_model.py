"""Loop notes cc's resolved model from system/init onto the provider.

`state.model` holds what the USER asked for (possibly a floating alias like
"opus"); the resolved id is display-only and must never overwrite it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.base import Provider
from synapse_core.providers.errors import ProviderDeadError
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class _ModelEmittingProvider(Provider):
    """Mock that emits a single init event carrying a model id, then result."""

    def __init__(self, model_id: str | None = "claude-opus-4-7[1m]") -> None:
        self.alive = False
        self._model = model_id
        self._queue: deque[dict[str, Any]] = deque()
        self.usage_total: dict[str, int] = {}
        self.session_id: str | None = None
        self.model_actual: str | None = None

    def spawn(self, env: dict[str, str] | None = None) -> None:
        self.alive = True

    def send(self, msg: str) -> None:
        if not self.alive:
            raise ProviderDeadError("not spawned")
        init: dict[str, Any] = {
            "type": "system",
            "subtype": "init",
            "session_id": "stub-sid-0001",
        }
        if self._model is not None:
            init["model"] = self._model
        self._queue.append(init)
        self._queue.append({"type": "result", "session_id": "stub-sid-0001"})

    def recv(self) -> Iterator[dict[str, Any]]:
        if not self.alive:
            raise ProviderDeadError("not spawned")
        while self._queue:
            ev = self._queue.popleft()
            yield ev
            if ev.get("type") == "result":
                return

    def cancel(self) -> None:
        self._queue.clear()

    def close(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


class _SilentILink:
    def poll_messages(self) -> list[dict]:
        return []

    def send_text(self, *_a, **_k) -> bool:
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


@pytest.fixture()
def loop_env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    clock = lambda: 1000.0  # noqa: E731
    buf = InboundBuffer(clock=clock)
    loop = MainLoop(
        ilink=_SilentILink(),
        provider_factory=_ModelEmittingProvider,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=buf,
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 2, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    loop._provider = _ModelEmittingProvider()
    loop._provider.spawn()
    return loop, state


def test_init_event_notes_model_on_provider(loop_env) -> None:
    loop, state = loop_env
    state.model = "opus"
    loop._provider.send("ping")
    loop._drain_recv()
    assert loop._provider.model_actual == "claude-opus-4-7[1m]"
    # The user's floating alias survives — never re-pinned to the resolved id.
    assert state.model == "opus"
    assert state.session_id == "stub-sid-0001"


def test_init_event_without_model_leaves_model_actual_none(tmp_path: Path) -> None:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    clock = lambda: 1000.0  # noqa: E731
    factory = lambda **_kw: _ModelEmittingProvider(model_id=None)  # noqa: E731
    loop = MainLoop(
        ilink=_SilentILink(),
        provider_factory=factory,
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
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )
    loop._provider = factory()
    loop._provider.spawn()
    loop._provider.send("ping")
    loop._drain_recv()
    assert loop._provider.model_actual is None
    assert state.model is None
    assert state.session_id == "stub-sid-0001"
