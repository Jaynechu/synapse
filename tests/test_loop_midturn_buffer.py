"""Mid-turn inbound: the reply always ships; messages that arrived while cc
was producing the reply stay in the InboundBuffer and become the next turn.

Replaces the old pre-send merge behavior (reply dropped + re-queued).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.base import Provider
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
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        return []

    def send_text(self, to_user_id: str, ctx_token: str, text: str, **_) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    def send_typing(self, *a, **k) -> None:
        return None

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class InjectingProvider(Provider):
    """Adds a bubble to the loop buffer during recv(), simulating new inbound
    arriving while the provider is producing its reply."""

    def __init__(self, reply: str, loop_ref: list) -> None:
        self._reply = reply
        self._loop_ref = loop_ref
        self._alive = True

    def spawn(self, env=None) -> None:
        self._alive = True

    def send(self, prompt: str) -> None:
        return None

    def recv(self):
        loop: MainLoop = self._loop_ref[0]
        with loop._state_lock:
            loop._buffer.add("new bubble mid-turn")
        yield {"type": "system", "subtype": "init", "session_id": "mock-sid-mid"}
        yield {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": self._reply}],
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        }
        yield {"type": "result", "result": self._reply, "session_id": "mock-sid-mid"}

    def close(self) -> None:
        self._alive = False

    def cancel(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


@pytest.fixture()
def env(tmp_path: Path):
    return {
        "state": BridgeState(),
        "sessions": SessionTracker(state_path=tmp_path / "sessions.json"),
        "tmp": tmp_path,
    }


def _make_loop(env, ilink, provider) -> tuple[MainLoop, FakeClock]:
    clock = FakeClock()
    fixed = datetime(2026, 6, 12, 12, 0)
    loop = MainLoop(
        ilink=ilink,
        provider_factory=lambda: provider,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: fixed,
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
    )
    loop._provider = provider
    loop._provider.spawn()
    return loop, clock


def test_reply_ships_even_when_buffer_nonempty_after_drain(env) -> None:
    """New inbound arrives during recv → reply STILL sent; mid-turn bubble
    stays in the buffer for the next turn (no drop, no merge)."""
    ilink = FakeILink()
    loop_ref: list = []
    provider = InjectingProvider("the reply", loop_ref)
    loop, clock = _make_loop(env, ilink, provider)
    loop_ref.append(loop)

    with loop._state_lock:
        loop._buffer.add("original message")
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"

    clock.advance(6.0)
    loop.maybe_flush()

    # Reply shipped despite the mid-turn bubble.
    assert ilink.sent, "Expected the reply to ship"
    assert ilink.sent[0][2] == "the reply"

    # The mid-turn bubble survives untouched for the next turn.
    assert len(loop._buffer) == 1
    flushed = loop._buffer.flush()
    assert flushed == "new bubble mid-turn"


def test_no_new_inbound_reply_sent_normally(env) -> None:
    """Regression: no new inbound during recv → reply sent, buffer empty."""
    ilink = FakeILink()
    provider = EchoProvider()
    loop, clock = _make_loop(env, ilink, provider)

    with loop._state_lock:
        loop._buffer.add("hello")
        loop._last_from_wxid = "lumi"
        loop._last_ctx_token = "ctx-1"

    clock.advance(6.0)
    loop.maybe_flush()

    assert ilink.sent, "Expected reply bubbles to be sent"
    assert ilink.sent[0][0] == "lumi"
    assert len(loop._buffer) == 0
