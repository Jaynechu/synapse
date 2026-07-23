"""Regression for the wx flush/idle-listener stdout-queue race (commits
e975e62, f6540c3).

Root cause: the flush path (maybe_flush -> provider.send + _drain_recv) held NO
lock across recv, while the idle listener held _state_lock around poll_line.
Because the flush path never took that lock, both threads blocked on the SAME
provider stdout queue and split one turn's lines between them — neither saw
`result`, so the solicited recv hit EOF-without-result ("subprocess died during
recv"). Fix: a dedicated _recv_lock held across the WHOLE solicited turn
(send->drain->retry) and across the listener's poll_line+drain, giving a
single-consumer guarantee at the loop layer (mirrors tg's asyncio.Lock).

This test proves the race is closed: while the flush path is mid-turn (blocked
between two lines of the SAME turn), the listener attempts _listen_once and MUST
NOT consume any line — every line of the turn goes to the solicited consumer.

Mock at the provider boundary only; never spawn claude; TypingPing stubbed.
"""

from __future__ import annotations

import threading
import time

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.config import Config
from synapse_wx.loop import MainLoop


class FakeILink:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.typing = 0

    def send_text(self, to_user_id, ctx_token, text, **_kwargs) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    def send_typing(self, *_a, **_k) -> None:
        self.typing += 1


class _StubTyping:
    def __init__(self, ilink, to_user_id, context_token, interval=5.0) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def stub_typing(monkeypatch):
    monkeypatch.setattr("synapse_wx.loop.TypingPing", _StubTyping)


@pytest.fixture(autouse=True)
def one_bubble_split(monkeypatch):
    monkeypatch.setattr(
        "synapse_wx.loop.split_for_wechat_typed",
        lambda text: [{"kind": "text", "text": text}],
    )


class BlockingTurnProvider:
    """A single solicited turn split across events. recv() yields the assistant
    text, then BLOCKS on `mid_turn` (simulating cc still streaming) until the
    test releases it, then yields the result. Every consumed event is recorded
    with the consuming thread name so the test can prove ownership.

    poll_line() records the calling thread too — if the listener manages to pull
    a line while the flush path is mid-turn, it shows up here and the assertion
    fails.
    """

    def __init__(self) -> None:
        self.alive = True
        self.session_id = None
        self.turn_output_capped = False
        self.usage_total: dict = {}
        self.mid_turn = threading.Event()  # flush blocks here mid-turn
        self.entered_recv = threading.Event()  # flush has started consuming
        self.consumers: list[str] = []  # thread name per consumed event
        self.poll_callers: list[str] = []
        self._recv_started = False

    def send(self, msg):
        return None

    def poll_line(self, timeout):
        # The listener calls this. Record who called and return None (no work)
        # — but the record is what proves the listener did (or did not) get in.
        self.poll_callers.append(threading.current_thread().name)
        return None

    def recv(self, first_line=None):
        who = threading.current_thread().name
        # Solicited turn: init -> assistant text -> [BLOCK] -> result.
        self.consumers.append(f"{who}:init")
        yield {"type": "system", "subtype": "init", "session_id": "sid-race"}
        self.consumers.append(f"{who}:assistant")
        yield {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "solicited reply"}]},
        }
        # Mid-turn: signal we're consuming, then block so a competing listener
        # would (pre-fix) race in on the same queue right here.
        self.entered_recv.set()
        self.mid_turn.wait(timeout=5.0)
        self.consumers.append(f"{who}:result")
        yield {"type": "result", "result": "solicited reply"}

    def is_alive(self):
        return self.alive


def _loop(tmp_path) -> MainLoop:
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    loop = MainLoop(
        ilink=FakeILink(),
        provider_factory=lambda *_a, **_k: None,
        state=state,
        sessions=sessions,
        idle_loop=None,
        buffer=InboundBuffer(),
        poll_interval_sec=0.01,
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        alerts=None,
        cfg=Config(),
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        media_dir=tmp_path / "media",
    )
    loop._last_from_wxid = "lumi"
    loop._last_ctx_token = "ctx-1"
    return loop


def test_listener_cannot_consume_while_flush_mid_turn(tmp_path):
    """The core regression: while maybe_flush is mid-turn holding _recv_lock, an
    idle-listener iteration MUST NOT touch the provider stdout queue. All of the
    turn's events belong to the flush thread; the listener's poll_line never
    runs until the flush releases the lock."""
    loop = _loop(tmp_path)
    prov = BlockingTurnProvider()
    loop._provider = prov
    # Prime a ready buffer so maybe_flush actually runs a turn.
    loop._buffer.add("hello from lumi")
    # Force the buffer 'ready' regardless of debounce timing.
    loop._buffer.ready = lambda: True  # type: ignore[assignment]

    flush_done = threading.Event()

    def run_flush():
        loop.maybe_flush()
        flush_done.set()

    flush_t = threading.Thread(target=run_flush, name="flush-thread")
    flush_t.start()

    # Wait until the flush path is provably mid-turn (blocked in recv holding
    # _recv_lock), then fire a listener iteration.
    assert prov.entered_recv.wait(timeout=5.0), "flush never reached mid-turn"

    listener_returned = threading.Event()

    def run_listener():
        loop._listen_once()  # must block on _recv_lock, consume nothing
        listener_returned.set()

    listener_t = threading.Thread(target=run_listener, name="listener-thread")
    listener_t.start()

    # Give the listener a real chance to (wrongly) race in on the queue.
    time.sleep(0.3)

    # PROOF the race is closed: while flush is still mid-turn, the listener has
    # NOT called poll_line and has NOT consumed any event — it is parked on
    # _recv_lock. No event was consumed by the listener thread.
    assert prov.poll_callers == [], (
        f"listener touched the queue mid-turn: {prov.poll_callers}"
    )
    assert all(c.startswith("flush-thread:") for c in prov.consumers), (
        f"an event leaked to a non-flush consumer: {prov.consumers}"
    )
    assert not listener_returned.is_set(), "listener returned before flush released lock"

    # Release the turn; flush finishes, delivers, then the listener may run.
    prov.mid_turn.set()
    assert flush_done.wait(timeout=5.0), "flush thread hung"
    flush_t.join(timeout=5.0)
    listener_t.join(timeout=5.0)

    # Solicited reply delivered exactly once, whole turn consumed by flush.
    assert [s[2] for s in loop._ilink.sent] == ["solicited reply"]
    assert prov.consumers == [
        "flush-thread:init",
        "flush-thread:assistant",
        "flush-thread:result",
    ]
    # After release the listener ran its poll_line exactly once (found nothing).
    assert prov.poll_callers == ["listener-thread"]
