"""Typing indicator: lazy-fires right before provider.send, stops after first reply bubble."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synapse_wx import typing_ping as tp_mod
from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop
from synapse_core.providers.base import Provider
from synapse_core.providers.errors import ProviderDeadError
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.typing_ping import TypingPing


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeILink:
    def __init__(self, inbound_batches: list[list[dict]]) -> None:
        self._batches = list(inbound_batches)
        self.sent: list[tuple[str, str, str]] = []
        self.send_typing = MagicMock()

    def poll_messages(self) -> list[dict]:
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_text(
        self, to_user_id: str, ctx_token: str, text: str, **_kwargs
    ) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class SlowEchoProvider(EchoProvider):
    """EchoProvider that sleeps inside recv() so the ping thread can tick."""

    def __init__(self, delay: float = 0.15) -> None:
        super().__init__()
        self._delay = delay

    def recv(self):
        time.sleep(self._delay)
        yield from super().recv()


class DeadProvider(Provider):
    """Provider whose recv() raises ProviderDeadError on first call."""

    def __init__(self) -> None:
        self._alive = True

    def spawn(self, env=None) -> None:
        self._alive = True

    def send(self, prompt: str) -> None:
        return None

    def recv(self):
        raise ProviderDeadError("boom")
        yield

    def close(self) -> None:
        self._alive = False

    def cancel(self) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


class EmptyProvider(Provider):
    """Provider whose recv() yields no assistant text."""

    def __init__(self) -> None:
        self._alive = True

    def spawn(self, env=None) -> None:
        self._alive = True

    def send(self, prompt: str) -> None:
        return None

    def recv(self):
        return
        yield

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


def _make_loop(env, ilink, clock, provider):
    fixed = datetime(2026, 6, 2, 12, 0)
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
    return loop


def test_typing_does_not_fire_during_debounce(env, monkeypatch) -> None:
    """Lazy: tick() alone must NOT start typing — bridge is just buffering."""
    original_init = TypingPing.__init__

    def short_init(self, ilink, to, ctx, interval=5.0):
        original_init(self, ilink, to, ctx, interval=0.05)

    monkeypatch.setattr(tp_mod.TypingPing, "__init__", short_init)

    inbound = [[{"from_wxid": "testuser", "context_token": "ctx-1", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock(start=1000.0)
    loop = _make_loop(env, ilink, clock, SlowEchoProvider(delay=0.0))

    loop.tick()
    # Debounce window not elapsed → no flush, no typing.
    assert loop._typing_ping is None
    assert ilink.send_typing.call_count == 0


def test_typing_fires_when_provider_send_invoked(env, monkeypatch) -> None:
    """Lazy: typing starts when maybe_flush actually drives provider.send."""
    original_init = TypingPing.__init__

    def short_init(self, ilink, to, ctx, interval=5.0):
        original_init(self, ilink, to, ctx, interval=0.05)

    monkeypatch.setattr(tp_mod.TypingPing, "__init__", short_init)

    inbound = [[{"from_wxid": "testuser", "context_token": "ctx-1", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock(start=1000.0)
    # Slow provider so we can observe the ping starting before recv finishes.
    loop = _make_loop(env, ilink, clock, SlowEchoProvider(delay=0.0))

    # Capture the moment typing first fires by recording state inside send.
    typing_at_send: list[bool] = []
    real_send = loop._provider.send

    def tracking_send(prompt):
        typing_at_send.append(loop._typing_ping is not None)
        # And confirm the indicator ping fired.
        typing_at_send.append(ilink.send_typing.call_count >= 1)
        return real_send(prompt)

    loop._provider.send = tracking_send  # type: ignore[assignment]

    loop.tick()
    # Confirm still not pinging mid-debounce.
    assert ilink.send_typing.call_count == 0
    assert loop._typing_ping is None

    clock.advance(6.0)
    loop.maybe_flush()

    # Typing was live by the time send() ran.
    assert typing_at_send == [True, True]
    args, _ = ilink.send_typing.call_args_list[0]
    assert args[0] == "testuser"
    assert args[1] == "ctx-1"
    # And it stops after the first reply bubble.
    assert loop._typing_ping is None
    time.sleep(0.15)
    pings_after = ilink.send_typing.call_count
    time.sleep(0.15)
    assert ilink.send_typing.call_count == pings_after


def test_typing_single_pinger_across_debounce_window(env, monkeypatch) -> None:
    """Two inbound bubbles inside the same turn → only one TypingPing started."""
    original_init = TypingPing.__init__

    def short_init(self, ilink, to, ctx, interval=5.0):
        original_init(self, ilink, to, ctx, interval=10.0)  # long, no auto-pings

    monkeypatch.setattr(tp_mod.TypingPing, "__init__", short_init)

    inbound = [
        [{"from_wxid": "testuser", "context_token": "ctx-1", "text": "first"}],
        [{"from_wxid": "testuser", "context_token": "ctx-1", "text": "second"}],
    ]
    ilink = FakeILink(inbound)
    clock = FakeClock(start=1000.0)
    loop = _make_loop(env, ilink, clock, SlowEchoProvider(delay=0.0))

    loop.tick()
    loop.tick()
    # Two inbound polls inside the debounce window: still no typing.
    assert loop._typing_ping is None
    assert ilink.send_typing.call_count == 0

    clock.advance(6.0)
    loop.maybe_flush()

    # Exactly one immediate ping from the single start fired in maybe_flush.
    assert ilink.send_typing.call_count == 1
    # And it stops cleanly after the reply.
    assert loop._typing_ping is None


def test_typing_stops_after_first_reply_bubble(env, monkeypatch) -> None:
    original_init = TypingPing.__init__

    def short_init(self, ilink, to, ctx, interval=5.0):
        original_init(self, ilink, to, ctx, interval=0.05)

    monkeypatch.setattr(tp_mod.TypingPing, "__init__", short_init)

    inbound = [[{"from_wxid": "testuser", "context_token": "ctx-1", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock(start=1000.0)
    loop = _make_loop(env, ilink, clock, SlowEchoProvider(delay=0.0))

    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()

    assert loop._typing_ping is None
    # First reply bubble should have been sent.
    assert any(s[0] == "testuser" for s in ilink.sent)


def test_typing_stops_on_empty_reply(env, monkeypatch) -> None:
    original_init = TypingPing.__init__

    def short_init(self, ilink, to, ctx, interval=5.0):
        original_init(self, ilink, to, ctx, interval=0.05)

    monkeypatch.setattr(tp_mod.TypingPing, "__init__", short_init)

    inbound = [[{"from_wxid": "testuser", "context_token": "ctx-1", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock(start=1000.0)
    loop = _make_loop(env, ilink, clock, EmptyProvider())

    loop.tick()
    # Still no typing mid-debounce.
    assert loop._typing_ping is None
    clock.advance(6.0)
    loop.maybe_flush()

    assert loop._typing_ping is None
    assert ilink.sent == []


def test_typing_stops_on_provider_dead(env, monkeypatch) -> None:
    original_init = TypingPing.__init__

    def short_init(self, ilink, to, ctx, interval=5.0):
        original_init(self, ilink, to, ctx, interval=0.05)

    monkeypatch.setattr(tp_mod.TypingPing, "__init__", short_init)

    inbound = [[{"from_wxid": "testuser", "context_token": "ctx-1", "text": "hi"}]]
    ilink = FakeILink(inbound)
    clock = FakeClock(start=1000.0)
    loop = _make_loop(env, ilink, clock, DeadProvider())

    loop.tick()
    # Still no typing mid-debounce.
    assert loop._typing_ping is None
    clock.advance(6.0)
    loop.maybe_flush()

    assert loop._typing_ping is None


def test_typing_noop_when_no_to_user(env) -> None:
    ilink = MagicMock()
    tp = TypingPing(ilink, "", "ctx-1", interval=0.05)
    tp.start()
    time.sleep(0.12)
    tp.stop()
    assert ilink.send_typing.call_count == 0


def test_typing_swallows_ilink_errors() -> None:
    ilink = MagicMock()
    ilink.send_typing.side_effect = RuntimeError("boom")
    tp = TypingPing(ilink, "u1", "ctx", interval=0.05)
    tp.start()  # would raise if not swallowed
    time.sleep(0.08)
    tp.stop()
    # No exception escaped; at least the immediate ping was attempted.
    assert ilink.send_typing.call_count >= 1


def test_typing_stop_is_idempotent() -> None:
    ilink = MagicMock()
    tp = TypingPing(ilink, "u1", "ctx", interval=0.05)
    tp.start()
    tp.stop()
    tp.stop()  # second call is a no-op
    assert isinstance(tp._stop_evt, threading.Event)
