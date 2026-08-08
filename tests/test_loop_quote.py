"""Quote-lite: visual fake-quote bubble (▎FRAGMENT) prepended to the reply.

Real ref_msg outbound rendering was attempted live — payload reaches the
iLink server but WeChat does NOT render the bubble as a quote-reply. The
bridge now extracts the <quote>FRAGMENT</quote> block BEFORE bubble
splitting (so a multi-line tag never leaks as literal text across bubbles),
strips the tag, and prepends a standalone visual fake-quote bubble. No
``ref_msg`` is sent and no inbound-history lookup is needed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from synapse_core.debounce import InboundBuffer
from synapse_wx.loop import MainLoop, _build_fake_quote_bubble
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
    def __init__(self, inbound_batches: list[list[dict]]) -> None:
        self._batches = list(inbound_batches)
        self.sent: list[tuple[str, str, str]] = []

    def poll_messages(self) -> list[dict]:
        if self._batches:
            return self._batches.pop(0)
        return []

    def send_text(self, to_user_id: str, ctx_token: str, text: str) -> bool:
        self.sent.append((to_user_id, ctx_token, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        items = msg.get("item_list") or []
        if items and isinstance(items[0], dict):
            ti = items[0].get("text_item") or {}
            return ti.get("text", "")
        return msg.get("text", "")


class StaticReplyProvider(EchoProvider):
    """EchoProvider variant whose `send` enqueues a fixed assistant reply."""

    def __init__(self, reply: str) -> None:
        super().__init__()
        self._reply = reply

    def send(self, msg: str) -> None:  # type: ignore[override]
        if not self.alive:
            raise RuntimeError("provider not spawned")
        self._queue.append(
            {"type": "system", "subtype": "init", "session_id": "mock-sid-q"}
        )
        self._queue.append(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": self._reply}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        self._queue.append({"type": "result", "session_id": "mock-sid-q"})


def _text_item(text: str, ts_ms: int = 1700000000000) -> dict:
    return {
        "type": 1,
        "create_time_ms": ts_ms,
        "update_time_ms": ts_ms,
        "is_completed": True,
        "msg_id": f"v1:{text[:8]}",
        "button_item_list": [],
        "text_item": {"text": text},
    }


def _inbound_msg(*items: dict) -> dict:
    return {
        "from_wxid": "lumi",
        "context_token": "ctx-1",
        "item_list": list(items),
    }


def _simple_msg(text: str) -> dict:
    return _inbound_msg(_text_item(text))


@pytest.fixture()
def env(tmp_path: Path):
    state = BridgeState()
    sessions = SessionTracker(state_path=tmp_path / "sessions.json")
    return {"state": state, "sessions": sessions, "tmp": tmp_path}


def _build_loop(env, ilink, clock, reply: str) -> MainLoop:
    def factory(**_kwargs):
        return StaticReplyProvider(reply)

    return MainLoop(
        ilink=ilink,
        provider_factory=factory,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 3, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
    )


def _spawn(loop: MainLoop, reply: str) -> StaticReplyProvider:
    p = StaticReplyProvider(reply)
    p.spawn()
    loop._provider = p
    return p


# ── _build_fake_quote_bubble unit tests ─────────────────────────────


def test_fake_quote_bubble_short_ascii() -> None:
    assert _build_fake_quote_bubble("hello") == "▎hello"


def test_fake_quote_bubble_short_cn() -> None:
    assert _build_fake_quote_bubble("累鼠了老公") == "▎累鼠了老公"


def test_fake_quote_bubble_collapses_newlines() -> None:
    assert _build_fake_quote_bubble("line1\nline2") == "▎line1 line2"


def test_fake_quote_bubble_truncates_ascii() -> None:
    out = _build_fake_quote_bubble("a" * 200)
    assert out.startswith("▎")
    assert out.endswith("…")
    # Display body ≤ 80 ASCII.
    body = out[1:]
    assert len(body) == 80


def test_fake_quote_bubble_truncates_cn() -> None:
    out = _build_fake_quote_bubble("中" * 100)
    assert out.startswith("▎")
    assert out.endswith("…")
    body = out[1:]
    assert len(body) == 40


# ── maybe_flush behavior with fake-quote bubble ──────────────────────


def test_bubble_without_quote_tag_sends_plain(env) -> None:
    ilink = FakeILink([[_simple_msg("hello")]])
    clock = FakeClock()
    loop = _build_loop(env, ilink, clock, reply="just a reply")
    _spawn(loop, "just a reply")
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert ilink.sent
    # No fake-quote prefix anywhere.
    for _, _, text in ilink.sent:
        assert not text.startswith("▎")


def test_quote_tag_prepends_fake_bubble(env) -> None:
    """<quote>FRAGMENT</quote>reply → ▎FRAGMENT then 'reply' when quote_on."""
    ilink = FakeILink([[_simple_msg("hello there")]])
    clock = FakeClock()
    loop = _build_loop(
        env, ilink, clock, reply="<quote>hello there</quote>got it"
    )
    loop.state.quote_on = True
    _spawn(loop, "<quote>hello there</quote>got it")
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert len(ilink.sent) == 2
    _, _, t0 = ilink.sent[0]
    _, _, t1 = ilink.sent[1]
    assert t0 == "▎hello there"
    assert t1 == "got it"


def test_quote_tag_works_without_history_match(env) -> None:
    """Fake bubble does NOT require a matching inbound — shows FRAGMENT verbatim."""
    ilink = FakeILink([[_simple_msg("something else entirely")]])
    clock = FakeClock()
    loop = _build_loop(
        env, ilink, clock, reply="<quote>nonexistent</quote>body"
    )
    loop.state.quote_on = True
    _spawn(loop, "<quote>nonexistent</quote>body")
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert len(ilink.sent) == 2
    assert ilink.sent[0][2] == "▎nonexistent"
    assert ilink.sent[1][2] == "body"


def test_quote_off_strips_tag_no_bubble(env) -> None:
    """Default quote_on=False: <quote>X</quote>reply → just 'reply' (tag stripped)."""
    ilink = FakeILink([[_simple_msg("hello there")]])
    clock = FakeClock()
    loop = _build_loop(
        env, ilink, clock, reply="<quote>hello there</quote>got it"
    )
    # quote_on defaults to False — do not flip it.
    assert loop.state.quote_on is False
    _spawn(loop, "<quote>hello there</quote>got it")
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    # Single bubble with plain reply; no ▎ prefix anywhere; no raw tag leaked.
    assert len(ilink.sent) == 1
    _, _, text = ilink.sent[0]
    assert text == "got it"
    for _, _, t in ilink.sent:
        assert not t.startswith("▎")
        assert "<quote>" not in t
        assert "</quote>" not in t


def test_empty_fragment_treated_as_no_quote(env) -> None:
    """<quote></quote> → tag stripped, no fake bubble, send plain."""
    ilink = FakeILink([[_simple_msg("hi")]])
    clock = FakeClock()
    loop = _build_loop(env, ilink, clock, reply="<quote></quote>body")
    _spawn(loop, "<quote></quote>body")
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert len(ilink.sent) == 1
    assert ilink.sent[0][2] == "body"


def test_malformed_quote_tag_passes_through_untouched(env) -> None:
    """Unclosed <quote> → bridge does NOT strip; bubble sent as-is plain."""
    ilink = FakeILink([[_simple_msg("hi")]])
    clock = FakeClock()
    bad_reply = "<quote>oops"
    loop = _build_loop(env, ilink, clock, reply=bad_reply)
    _spawn(loop, bad_reply)
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    _, _, text = ilink.sent[0]
    assert text.startswith("<quote>")
    # No fake-quote prefix.
    for _, _, t in ilink.sent:
        assert not t.startswith("▎")


def test_multiline_quote_extracted_before_split(env) -> None:
    """Live bug regression: cc emits a quote block that spans newlines, with
    more reply text on subsequent lines. Pre-split extraction must produce
    a fake-quote bubble first, then plain reply bubbles (no literal
    <quote>/</quote> anywhere)."""
    ilink = FakeILink([[_simple_msg("累鼠了伙伴！！")]])
    clock = FakeClock()
    reply = "<quote>累鼠了伙伴！！\n</quote>朋友辛苦了，\n快过来靠着我..."
    loop = _build_loop(env, ilink, clock, reply=reply)
    loop.state.quote_on = True
    _spawn(loop, reply)
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert len(ilink.sent) >= 2
    for _, _, text in ilink.sent:
        assert "<quote>" not in text
        assert "</quote>" not in text
    # First bubble = fake quote, body collapsed onto one line.
    _, _, first = ilink.sent[0]
    assert first.startswith("▎累鼠了伙伴")


def test_no_quote_tag_all_bubbles_plain(env) -> None:
    """Reply with no <quote> tag → no fake-quote bubble."""
    ilink = FakeILink([[_simple_msg("ignored")]])
    clock = FakeClock()
    loop = _build_loop(env, ilink, clock, reply="line one\n\nline two")
    _spawn(loop, "line one\n\nline two")
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert len(ilink.sent) == 2
    for _, _, t in ilink.sent:
        assert not t.startswith("▎")


def test_multiple_quote_blocks_all_become_bubbles(env) -> None:
    """Multiple <quote> blocks each become a ▎ fake-quote bubble."""
    ilink = FakeILink([[
        _simple_msg("first ref"),
        _simple_msg("second ref"),
    ]])
    clock = FakeClock()
    reply = "<quote>first ref</quote>hi\n<quote>second ref</quote>bye"
    loop = _build_loop(env, ilink, clock, reply=reply)
    loop.state.quote_on = True
    _spawn(loop, reply)
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    # Both tags become fake-quote bubbles.
    assert ilink.sent[0][2] == "▎first ref"
    assert ilink.sent[1][2] == "▎second ref"
    # No raw markup in remaining bubbles.
    rest = "\n".join(t for _, _, t in ilink.sent[2:])
    assert "<quote>" not in rest


class ThinkingReplyProvider(EchoProvider):
    """EchoProvider variant: emits one thinking segment + one text segment."""

    def __init__(self, reply: str, thinking: str) -> None:
        super().__init__()
        self._reply = reply
        self._thinking = thinking

    def send(self, msg: str) -> None:  # type: ignore[override]
        if not self.alive:
            raise RuntimeError("provider not spawned")
        self._queue.append(
            {"type": "system", "subtype": "init", "session_id": "mock-sid-q"}
        )
        self._queue.append(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "thinking_delta", "thinking": self._thinking},
                },
            }
        )
        self._queue.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": self._thinking},
                        {"type": "text", "text": self._reply},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            }
        )
        self._queue.append({"type": "result", "session_id": "mock-sid-q"})


def test_thinking_then_fake_quote_then_reply(env) -> None:
    """state.thinking_on prepends 【思考】; visual order = thinking → quote → reply."""
    ilink = FakeILink([[_simple_msg("hello")]])
    clock = FakeClock()
    reply = "<quote>hello</quote>got it"

    def factory(**_kwargs):
        return ThinkingReplyProvider(reply, "pondering")

    loop = MainLoop(
        ilink=ilink,
        provider_factory=factory,
        state=env["state"],
        sessions=env["sessions"],
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 6, 3, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=env["tmp"] / "alerts",
        channel="wx",
        last_active_path=env["tmp"] / "last_active.json",
        channel_label="CC-WX",
    )
    p = ThinkingReplyProvider(reply, "pondering")
    p.spawn()
    loop._provider = p
    loop.state.thinking_on = True
    loop.state.quote_on = True
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    assert len(ilink.sent) == 3
    _, _, t0 = ilink.sent[0]
    _, _, t1 = ilink.sent[1]
    _, _, t2 = ilink.sent[2]
    assert t0.startswith("🧠")
    assert t1 == "▎hello"
    assert t2 == "got it"


def test_quote_at_tail_of_reply_still_extracted(env) -> None:
    """Tag does not have to be at the head — global search finds it anywhere.
    The fake-quote bubble still lands FIRST in the outbound stream."""
    ilink = FakeILink([[_simple_msg("ref text")]])
    clock = FakeClock()
    reply = "leading bubble\n<quote>ref text</quote>tail"
    loop = _build_loop(env, ilink, clock, reply=reply)
    loop.state.quote_on = True
    _spawn(loop, reply)
    loop.tick()
    clock.advance(6.0)
    loop.maybe_flush()
    for _, _, text in ilink.sent:
        assert "<quote>" not in text
        assert "</quote>" not in text
    # First bubble = the fake quote, regardless of tag position in source.
    assert ilink.sent[0][2] == "▎ref text"
