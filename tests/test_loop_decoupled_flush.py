"""Regression: flush thread must not be blocked by a long-poll-stuck tick.

Pre-fix, _run() called tick() then maybe_flush() in series on a single thread.
If poll_messages() parked in the iLink long-poll for ~20s, maybe_flush() was
delayed by that whole window — typing indicator landed 15s late.

This test pins the fix: with poll thread parked in a blocking poll_messages,
the flush thread alone must observe buffer.ready() and drive a provider turn.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState


class BlockingILink:
    """First poll returns one msg; subsequent polls park on `release_evt`."""

    def __init__(self, first_batch: list[dict]) -> None:
        self._first_batch = first_batch
        self._poll_count = 0
        self.release_evt = threading.Event()
        self.parked_evt = threading.Event()
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        self._poll_count += 1
        if self._poll_count == 1:
            return self._first_batch
        self.parked_evt.set()
        self.release_evt.wait(timeout=30.0)
        return []

    def send_text(self, to_user_id: str, ctx_token: str, text: str, **_) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


@pytest.fixture()
def env(tmp_path: Path):
    return {
        "state": BridgeState(),
        "sessions": SessionTracker(state_path=tmp_path / "sessions.json"),
        "tmp": tmp_path,
    }


def test_flush_fires_while_poll_thread_is_blocked(env) -> None:
    ilink = BlockingILink(
        [{"from_wxid": "testuser", "context_token": "ctx-1", "text": "hi"}]
    )
    # Real monotonic clock + a tight poll interval — we want the flush thread
    # to cycle fast so the assertion deadline can stay short.
    loop = MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.05,
        wallclock=lambda: datetime(2026, 6, 3, 2, 30),
        sleeper=time.sleep,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
    )
    # Shrink the debounce quiet window so the test stays under 1s wall time.
    loop._buffer.DEFAULT_QUIET_SEC = 0.2  # type: ignore[misc]

    loop.start()
    try:
        # Tick must absorb the msg AND then park in poll_messages.
        assert ilink.parked_evt.wait(timeout=2.0), "poll thread never parked"
        # Wait past the quiet window + a few flush cycles.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if ilink.sent:
                break
            time.sleep(0.05)
        assert ilink.sent, "flush did not fire while poll was blocked"
        assert ilink.sent[0][0] == "testuser"
    finally:
        ilink.release_evt.set()
        loop.stop()
