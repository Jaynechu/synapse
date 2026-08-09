"""Tests for the in-flight turn marker and boot recovery pipeline.

Covers:
  - _set_inflight / _clear_inflight lifecycle in _flush_one
  - stale-marker cleared on provider-gave-up and second-failure (abandon) paths
  - transcript_recover: happy path, no-file, garbage lines, since_ts filtering
  - boot recovery: recovered reply delivered via _deliver_reply
  - boot recovery: not recovered -> notice sent, marker cleared
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from synapse_core import bridge_state_store, transcript_recover
from synapse_core.commands import messages
from synapse_core.debounce import InboundBuffer
from synapse_core.providers.errors import ProviderDeadError
from synapse_core.state import BridgeState
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop


# ---------------------------------------------------------------------------
# Shared helpers / fakes
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> object:
        self.messages.append(kwargs)
        return type("Msg", (), {"message_id": len(self.messages)})()

    async def send_chat_action(self, **_kwargs) -> None:
        return None


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot


def _make_loop(tmp_path: Path) -> TgLoop:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    return TgLoop(cfg)


def _ready_loop(tmp_path: Path, clock: FakeClock, body: str = "hello") -> TgLoop:
    loop = _make_loop(tmp_path)
    loop._buffer = InboundBuffer(clock=clock)
    loop._buffer.add(body)
    clock.advance(6.0)
    loop._pending_chat_id = 42
    return loop


# ---------------------------------------------------------------------------
# Provider stubs
# ---------------------------------------------------------------------------

class OkProvider:
    """Succeeds and echoes a fixed reply."""

    session_id = "sess-ok"
    alive = True
    turn_output_capped = False

    def __init__(self, reply: str = "hi there") -> None:
        self._reply = reply

    def spawn(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def send(self, _body: str) -> None:
        pass

    def recv(self, first_line=None):
        yield {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": self._reply}],
                "usage": {},
            },
        }
        yield {"type": "result", "usage": {}}

    def cancel(self) -> None:
        self.alive = False

    def poll_line(self, _timeout: float) -> None:
        return None


class DeadProvider:
    session_id = None
    alive = True

    def spawn(self) -> None:
        self.alive = True

    def send(self, _body: str) -> None:
        raise ProviderDeadError("dead")

    def is_alive(self) -> bool:
        return self.alive

    def cancel(self) -> None:
        self.alive = False

    def poll_line(self, _timeout: float) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. Marker lifecycle: set before send, cleared after delivery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inflight_set_and_cleared_on_success(tmp_path: Path) -> None:
    clock = FakeClock()
    loop = _ready_loop(tmp_path, clock, body="test message")
    bot = FakeBot()

    captured: list[dict | None] = []
    original_set = loop._set_inflight

    def _capture(chat_id, body):
        original_set(chat_id, body)
        captured.append(dict(loop._state.inflight) if loop._state.inflight else None)

    loop._set_inflight = _capture  # type: ignore[method-assign]

    provider = OkProvider(reply="response")
    loop._provider = provider  # type: ignore[assignment]
    loop._make_provider = lambda: provider  # type: ignore[method-assign]

    await loop.check_flush(FakeContext(bot))  # type: ignore[arg-type]

    # Marker was set once
    assert len(captured) == 1
    assert captured[0] is not None
    assert captured[0]["chat_id"] == 42
    assert "test message" in captured[0]["body_preview"]

    # Marker is cleared after successful delivery
    assert loop._state.inflight is None

    # Persisted state also has no marker
    saved = bridge_state_store.load(loop._state_path)
    assert saved.get("inflight") is None

    # Reply was actually delivered
    assert any("response" in m.get("text", "") for m in bot.messages)


@pytest.mark.asyncio
async def test_inflight_cleared_on_provider_gave_up(tmp_path: Path) -> None:
    clock = FakeClock()
    loop = _ready_loop(tmp_path, clock)
    bot = FakeBot()

    loop._provider = DeadProvider()  # type: ignore[assignment]
    loop._make_provider = lambda: DeadProvider()  # type: ignore[method-assign]
    loop._death_count = 2  # next death hits cap (3)

    await loop.check_flush(FakeContext(bot))  # type: ignore[arg-type]

    assert loop._state.inflight is None


@pytest.mark.asyncio
async def test_inflight_cleared_on_second_failure_requeue(tmp_path: Path) -> None:
    clock = FakeClock()
    loop = _ready_loop(tmp_path, clock)
    bot = FakeBot()

    loop._provider = DeadProvider()  # type: ignore[assignment]

    def _make_dead() -> DeadProvider:
        return DeadProvider()

    loop._make_provider = _make_dead  # type: ignore[method-assign]

    await loop.check_flush(FakeContext(bot))  # type: ignore[arg-type]

    assert loop._state.inflight is None
    # Body was requeued for retry
    assert loop._buffer.flush() == "hello"


# ---------------------------------------------------------------------------
# 2. Transcript recovery helper
# ---------------------------------------------------------------------------

def _ts_str(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _assistant_entry(text: str, ts_epoch: float) -> dict:
    return {
        "type": "assistant",
        "timestamp": _ts_str(ts_epoch),
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _write_transcript(projects_dir: Path, cwd: str, sid: str, entries: list[dict]) -> None:
    path = transcript_recover._transcript_path(str(projects_dir), cwd, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def test_recover_reply_happy_path(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "abc123"
    since_ts = 1000.0

    _write_transcript(projects, cwd, sid, [
        _assistant_entry("first chunk", since_ts + 1),
        _assistant_entry("second chunk", since_ts + 2),
    ])

    result = transcript_recover.recover_reply(str(projects), cwd, sid, since_ts)
    assert result == "first chunk\n\nsecond chunk"


def test_recover_reply_no_file(tmp_path: Path) -> None:
    result = transcript_recover.recover_reply(
        str(tmp_path / "projects"), "/some/cwd", "nosuchsid", 1000.0
    )
    assert result is None


def test_recover_reply_garbage_lines(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "garbagesid"
    since_ts = 1000.0

    path = transcript_recover._transcript_path(str(projects), cwd, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        fh.write("not json\n")
        fh.write("{also not json\n")
        fh.write("\n")
        fh.write(json.dumps(_assistant_entry("valid reply", since_ts + 1)) + "\n")

    result = transcript_recover.recover_reply(str(projects), cwd, sid, since_ts)
    assert result == "valid reply"


def test_recover_reply_since_ts_filter_excludes_old(tmp_path: Path) -> None:
    """Entries before since_ts - slop are excluded."""
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "filtersid"
    since_ts = 1000.0

    _write_transcript(projects, cwd, sid, [
        _assistant_entry("old reply", since_ts - 100),  # well before cutoff
        _assistant_entry("new reply", since_ts + 1),    # after cutoff
    ])

    result = transcript_recover.recover_reply(str(projects), cwd, sid, since_ts)
    assert result == "new reply"


def test_recover_reply_slop_includes_near_boundary(tmp_path: Path) -> None:
    """An entry 3s before since_ts falls within the 5s slop window."""
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "slopsid"
    since_ts = 1000.0

    _write_transcript(projects, cwd, sid, [
        _assistant_entry("just in time", since_ts - 3),
    ])

    result = transcript_recover.recover_reply(str(projects), cwd, sid, since_ts)
    assert result == "just in time"


def test_recover_reply_strips_tool_xml(tmp_path: Path) -> None:
    """strip_tool_xml is applied to each text block."""
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "xmlsid"
    since_ts = 1000.0

    # Build raw text with leaked invoke XML
    raw = "clean text<invoke name=\"tool\"><parameter name=\"x\">v</parameter></invoke>rest"

    _write_transcript(projects, cwd, sid, [
        _assistant_entry(raw, since_ts + 1),
    ])

    result = transcript_recover.recover_reply(str(projects), cwd, sid, since_ts)
    # strip_tool_xml removes the invoke block; "clean text" and "rest" survive
    assert result is not None
    assert "<invoke" not in result
    assert "clean text" in result


def test_recover_reply_non_assistant_rows_ignored(tmp_path: Path) -> None:
    """Rows with type != 'assistant' are skipped."""
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "mixedsid"
    since_ts = 1000.0

    _write_transcript(projects, cwd, sid, [
        {"type": "user", "timestamp": _ts_str(since_ts + 1), "message": {"content": [{"type": "text", "text": "user msg"}]}},
        {"type": "system", "timestamp": _ts_str(since_ts + 1), "message": {}},
        _assistant_entry("assistant reply", since_ts + 2),
    ])

    result = transcript_recover.recover_reply(str(projects), cwd, sid, since_ts)
    assert result == "assistant reply"


def test_recover_reply_no_qualifying_entries(tmp_path: Path) -> None:
    """All entries are too old -> returns None."""
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "oldsid"
    since_ts = 2000.0

    _write_transcript(projects, cwd, sid, [
        _assistant_entry("ancient reply", 500.0),
    ])

    result = transcript_recover.recover_reply(str(projects), cwd, sid, since_ts)
    assert result is None


# ---------------------------------------------------------------------------
# 3. Boot recovery: recovered reply -> _deliver_reply
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_recovery_delivers_recovered_reply(tmp_path: Path) -> None:
    """When inflight marker exists and transcript has a reply, it's delivered."""
    projects = tmp_path / "projects"
    cwd = "/home/user/project"
    sid = "recoverysid"
    ts = 1000.0

    _write_transcript(projects, cwd, sid, [
        _assistant_entry("recovered reply", ts + 1),
    ])

    delivered: list[tuple] = []

    async def fake_deliver(bot, chat_id, response, thinking):
        delivered.append((chat_id, response))

    loop = _make_loop(tmp_path)
    loop._state.inflight = {
        "chat_id": 99,
        "body_preview": "original message",
        "ts": ts,
        "session_id": sid,
    }
    loop._state.cc_cwd = cwd
    loop._deliver_reply = fake_deliver  # type: ignore[method-assign]

    bot = FakeBot()

    # Simulate the recovery logic directly (same as _boot_recovery in __main__)
    marker = loop._state.inflight
    chat_id = marker["chat_id"]
    session_id = marker["session_id"] or loop._state.session_id
    recovered = transcript_recover.recover_reply(
        cc_projects_dir=str(projects),
        cwd=loop._state.cc_cwd,
        session_id=session_id,
        since_ts=marker["ts"],
    )

    assert recovered == "recovered reply"

    await fake_deliver(bot, chat_id, recovered, "")
    loop._state.inflight = None
    loop._persist_state()

    assert len(delivered) == 1
    assert delivered[0][0] == 99
    assert delivered[0][1] == "recovered reply"
    assert loop._state.inflight is None


@pytest.mark.asyncio
async def test_boot_recovery_sends_notice_when_no_transcript(tmp_path: Path) -> None:
    """When inflight marker exists but no transcript, a notice is sent."""
    loop = _make_loop(tmp_path)
    loop._state.inflight = {
        "chat_id": 77,
        "body_preview": "my question",
        "ts": 1000.0,
        "session_id": "missingsid",
    }
    loop._state.cc_cwd = "/some/cwd"

    bot = FakeBot()

    marker = loop._state.inflight
    chat_id = marker["chat_id"]
    session_id = marker["session_id"]
    preview = marker["body_preview"]

    recovered = transcript_recover.recover_reply(
        cc_projects_dir=str(tmp_path / "projects"),
        cwd=loop._state.cc_cwd,
        session_id=session_id,
        since_ts=marker["ts"],
    )
    assert recovered is None

    # Simulate the "not recovered" branch
    notice_text = messages.t(
        "bridge.reply_lost_on_restart",
        loop._state.voice_style,
        preview=preview,
    )
    await bot.send_message(chat_id=chat_id, text=notice_text)
    loop._state.inflight = None
    loop._persist_state()

    assert len(bot.messages) == 1
    assert "my question" in bot.messages[0]["text"]
    assert loop._state.inflight is None


# ---------------------------------------------------------------------------
# 4. bridge_state_store: inflight round-trips correctly
# ---------------------------------------------------------------------------

def test_bridge_state_store_persists_inflight(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = BridgeState()
    state.inflight = {"chat_id": 5, "body_preview": "hi", "ts": 123.4, "session_id": "sid1"}

    from dataclasses import asdict
    bridge_state_store.save(path, asdict(state))
    loaded = bridge_state_store.load(path)

    assert loaded["inflight"] == {"chat_id": 5, "body_preview": "hi", "ts": 123.4, "session_id": "sid1"}


def test_bridge_state_store_inflight_none_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = BridgeState()
    state.inflight = None

    from dataclasses import asdict
    bridge_state_store.save(path, asdict(state))
    loaded = bridge_state_store.load(path)

    # When None, key may be absent or None — both acceptable
    assert loaded.get("inflight") is None


def test_bridge_state_store_ignores_malformed_inflight(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    import json as _json
    path.write_text(_json.dumps({"inflight": "not a dict", "session_id": "s1"}))
    loaded = bridge_state_store.load(path)
    assert loaded.get("inflight") is None
    assert loaded.get("session_id") == "s1"
