"""T9: tg bridge as a cortex shell host — silence cycle, wake ledger, fuse.

Every real boundary is mocked: no claude spawn (feed_turn stubbed or a
QueueProvider), no cortex subprocess (note render stubbed), no telegram bot,
no real sleeps. The scheduler runs with an injected clock where used.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from synapse_core import shell_state
from synapse_tg.config import TgConfig, load_config
from synapse_tg.loop import TgLoop
from synapse_tg.shell import ShellHost, occupancy, parse_wake_at

MIN = 60.0


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return type("M", (), {"message_id": len(self.sent)})()

    async def send_chat_action(self, **_):
        return None


class _StubTyping:
    running = True

    def __init__(self, bot, chat_id) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def stub_typing(monkeypatch):
    monkeypatch.setattr("synapse_tg.loop.TypingAction", _StubTyping)


@pytest.fixture
def short_sock():
    """A socket path short enough for the macOS 104-byte AF_UNIX cap."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="shl", dir="/tmp")
    try:
        yield Path(d) / "s.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


class Clock:
    def __init__(self, t=1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _cfg(tmp_path, **kw):
    base = dict(
        data_dir=tmp_path / "tg-data",
        shell_enabled=True,
        shell_state_dir=str(tmp_path / "shells"),
        shell_socket=str(tmp_path / "s.sock"),
        shell_idle_min=20.0,
        shell_note_render_cmd=["true"],
    )
    base.update(kw)
    return TgConfig(**base)


def _host(tmp_path, clock, *, feeds=None, **kw):
    """ShellHost over a real TgLoop whose feed_turn/respawn are recorded."""
    cfg = _cfg(tmp_path, **kw)
    loop = TgLoop(cfg)
    fed = feeds if feeds is not None else []

    async def _feed(body):
        fed.append(body)
        return True

    loop.feed_turn = _feed
    loop.shell_respawn = lambda: fed.append("__RESPAWN__")
    host = ShellHost(cfg, loop, clock=clock)
    loop.attach_shell(host)
    host._render_note = lambda: "NOTE BODY"
    return host, loop, fed


# ── occupancy / ledger parsing ────────────────────────────────────────────────

def test_occupancy_sums_the_four_cortex_keys():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 100,
             "cache_creation_input_tokens": 5, "output_tokens": 1,
             "server_tool_use": {"x": 1}}
    assert occupancy(usage) == 116
    assert occupancy(None) == 0


def test_parse_wake_at_accepts_offset_and_naive():
    assert parse_wake_at("2026-07-25T10:00:00+10:00") == 1784937600.0
    naive = parse_wake_at("2026-07-25T10:00:00")
    assert naive is not None and abs(naive - 1784937600.0) < 86400
    assert parse_wake_at("nonsense") is None
    assert parse_wake_at("") is None
    assert parse_wake_at(None) is None


# ── silence cycle ─────────────────────────────────────────────────────────────

def test_silence_elapse_feeds_exactly_one_note_turn(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 1
    assert fed[0].startswith("⏳ [NEW ROUND]")
    assert "NOTE BODY" in fed[0]
    st = shell_state.read(tmp_path / "shells", "tg")
    assert st["last_note_ts"]


def test_user_message_mid_cycle_resets_timer_without_feeding(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)

    async def run():
        host._arm()
        clock.t += 19 * MIN
        host.on_user_message()          # reset at t+19min
        clock.t += 5 * MIN              # t+24min: 5min into the new cycle
        await host._fire("tg")          # scheduler pass (not yet due)

    asyncio.run(run())
    assert fed == []


def test_second_cycle_repeats_after_a_fed_round(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 2


def test_render_failure_skips_the_round_without_crashing(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    host._render_note = lambda: None

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert fed == []
    assert shell_state.read(tmp_path / "shells", "tg").get("last_note_ts") is None


def test_render_cmd_unset_skips_the_round(tmp_path):
    cfg = _cfg(tmp_path, shell_note_render_cmd=[])
    host = ShellHost(cfg, TgLoop(cfg), clock=Clock())
    assert host._render_note() is None


# ── wake ledger ───────────────────────────────────────────────────────────────

def test_due_next_wake_at_feeds_a_note_and_consumes_the_ledger(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    from datetime import datetime, timezone
    due = datetime.fromtimestamp(clock.t + 2 * MIN, timezone.utc).isoformat()
    shell_state.write(tmp_path / "shells", "tg", {"next_wake_at": due})

    async def run():
        host._arm()
        clock.t += 3 * MIN              # past next_wake_at, well before idle
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 1
    assert "next_wake_at" not in shell_state.read(tmp_path / "shells", "tg")


def test_kick_with_future_next_wake_at_only_rearms(tmp_path):
    """T7 kick lands on the same callback: a ledger entry announced for later
    must re-arm, never feed."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    from datetime import datetime, timezone
    later = datetime.fromtimestamp(clock.t + 5 * MIN, timezone.utc).isoformat()
    shell_state.write(tmp_path / "shells", "tg", {"next_wake_at": later})

    asyncio.run(host._fire("tg"))
    assert fed == []
    assert host._scheduler._table["tg"][0] == pytest.approx(clock.t + 5 * MIN, abs=1)


def test_lie_down_zero_wakes_immediately(tmp_path):
    """lie_down(0) writes next_wake_at = now -> the kicked pass feeds at once."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    from datetime import datetime, timezone
    now_iso = datetime.fromtimestamp(clock.t, timezone.utc).isoformat()
    shell_state.write(tmp_path / "shells", "tg", {"next_wake_at": now_iso})

    asyncio.run(host._fire("tg"))
    assert len(fed) == 1


def test_kick_over_the_socket_reaches_the_scheduler(tmp_path, short_sock):
    """Real unix socket, mocked everything else: send_kick -> callback fires.
    The socket sits under a SHORT dir — pytest's tmp_path blows the macOS
    104-byte AF_UNIX cap (the T7 trap)."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock, shell_socket=str(short_sock))
    from datetime import datetime, timezone
    from synapse_core.scheduler import send_kick

    async def run():
        task = asyncio.create_task(host.run())   # armed 20min out, sleeping
        for _ in range(200):                     # wait for the socket to open
            if host._scheduler._server is not None:
                break
            await asyncio.sleep(0.005)
        # marrow's lie_down(0) shape: ledger due now, then poke the socket.
        shell_state.write(tmp_path / "shells", "tg", {
            "next_wake_at": datetime.fromtimestamp(clock.t, timezone.utc).isoformat()})
        await send_kick(short_sock, "tg")
        for _ in range(200):
            if fed:
                break
            await asyncio.sleep(0.005)
        host.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())
    assert len(fed) == 1
    assert not short_sock.exists()               # socket cleaned up on stop


# ── directed kick (T10) ───────────────────────────────────────────────────────

def test_pending_note_is_fed_instead_of_the_rendered_note_and_cleared(tmp_path):
    """T10: marrow writes pending_note then kicks — the round feeds that text
    (machine tag kept) and the ledger key is gone afterwards."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    shell_state.write(tmp_path / "shells", "tg", {"pending_note": "go check the diary"})

    asyncio.run(host._fire("tg"))

    assert len(fed) == 1
    assert fed[0] == "⏳ [NEW ROUND]\ngo check the diary"
    assert "NOTE BODY" not in fed[0]
    assert "pending_note" not in shell_state.read(tmp_path / "shells", "tg")


def test_pending_note_fires_while_asleep_without_consuming_next_wake_at(tmp_path):
    """Asleep = a future next_wake_at. The direction fires now; the scheduled
    wake survives it."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    from datetime import datetime, timezone
    later = datetime.fromtimestamp(clock.t + 5 * MIN, timezone.utc).isoformat()
    shell_state.write(tmp_path / "shells", "tg",
                      {"next_wake_at": later, "pending_note": "wake up"})

    asyncio.run(host._fire("tg"))

    assert fed == ["⏳ [NEW ROUND]\nwake up"]
    st = shell_state.read(tmp_path / "shells", "tg")
    assert st["next_wake_at"] == later


def test_kick_lost_during_a_feed_is_recovered_by_the_rearm(tmp_path):
    """The scheduler drops a kick for a shell whose entry is mid-fire, so a
    direction written during a feed would be lost — the re-arm schedules it
    for now instead."""
    clock = Clock()
    fed: list[str] = []

    async def _feed(body):
        fed.append(body)
        # marrow's directed kick lands while this turn is still streaming.
        shell_state.write(tmp_path / "shells", "tg", {"pending_note": "late one"})
        return True

    host, loop, _ = _host(tmp_path, clock, feeds=fed)
    loop.feed_turn = _feed

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")               # silence round; kick lost mid-feed
        assert host._scheduler._table["tg"][0] == pytest.approx(clock.t, abs=1)
        await host._fire("tg")               # next scheduler pass

    asyncio.run(run())
    assert fed[1] == "⏳ [NEW ROUND]\nlate one"


def test_boot_arms_before_the_kick_socket_opens(tmp_path, short_sock):
    """A kick can never land on an empty table: run() arms, THEN listens."""
    clock = Clock()
    host, _loop, _fed = _host(tmp_path, clock, shell_socket=str(short_sock))

    async def run():
        task = asyncio.create_task(host.run())
        for _ in range(200):
            if host._scheduler._server is not None:
                break
            await asyncio.sleep(0.005)
        assert "tg" in host._scheduler._table
        host.stop()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(run())


def test_blank_pending_note_falls_back_to_the_rendered_note(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    shell_state.write(tmp_path / "shells", "tg", {"pending_note": "   "})

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert "NOTE BODY" in fed[0]


def test_take_reads_and_clears_in_one_pass(tmp_path):
    shell_state.write(tmp_path / "shells", "tg",
                      {"pending_note": "x", "session_id": "s"})
    assert shell_state.take(tmp_path / "shells", "tg", "pending_note") == "x"
    assert shell_state.take(tmp_path / "shells", "tg", "pending_note") is None
    assert shell_state.read(tmp_path / "shells", "tg")["session_id"] == "s"


# ── token ledger + fuse ───────────────────────────────────────────────────────

def test_after_turn_persists_occupancy_and_session_id(tmp_path):
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=180000)
    loop._state.session_id = "sess-1"
    loop._state.last_assistant_usage = {"input_tokens": 3,
                                        "cache_read_input_tokens": 4}
    asyncio.run(host.after_turn())
    st = shell_state.read(tmp_path / "shells", "tg")
    assert st["occupancy"] == 7 and st["session_id"] == "sess-1"
    assert fed == []


def test_occupancy_over_fuse_feeds_prompt_then_respawns(tmp_path):
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=100)
    loop._state.session_id = "sess-1"
    loop._state.last_assistant_usage = {"input_tokens": 150}
    asyncio.run(host.after_turn())
    assert fed[0].startswith("⚙️ [FUSE]")
    assert "lie_down(rotate=True)" in fed[0]
    assert fed[1] == "__RESPAWN__"
    st = shell_state.read(tmp_path / "shells", "tg")
    assert st["occupancy"] == 0 and "session_id" not in st


def test_fuse_disabled_at_zero(tmp_path):
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=0)
    loop._state.last_assistant_usage = {"input_tokens": 10**7}
    asyncio.run(host.after_turn())
    assert fed == []


def test_respawn_drops_session_and_keeps_queued_user_messages(tmp_path):
    """Fuse respawn: fresh session, InboundBuffer untouched (queued messages
    ride into the new session on the next flush)."""
    cfg = _cfg(tmp_path)
    loop = TgLoop(cfg)
    loop._state.session_id = "old-sid"
    loop._buffer.add("hello from mid-fuse")
    spawned: list = []

    class _P:
        alive = True
        session_id = None

        def spawn(self):
            spawned.append(loop._state.session_id)

        def cancel(self):
            pass

    loop._provider = _P()
    loop._make_provider = lambda: _P()
    loop.shell_respawn()
    assert loop._state.session_id is None
    assert spawned == [None]                     # fresh, no --resume sid
    assert loop._buffer.flush() == "hello from mid-fuse"


# ── feed_turn: the fed round's reply ships to tg like any other turn ──────────

class _FeedProvider:
    """Minimal provider: records what was sent, yields one scripted turn."""

    def __init__(self, reply="free round reply") -> None:
        self.alive = True
        self.session_id = "sess-fed"
        self.turn_output_capped = False
        self.sent: list[str] = []
        self._reply = reply

    def is_alive(self):
        return True

    def send(self, msg):
        self.sent.append(msg)

    def recv(self, first_line=None):
        yield {"type": "assistant",
               "message": {"content": [{"type": "text", "text": self._reply}],
                           "usage": {"input_tokens": 7}}}
        yield {"type": "result"}


def test_feed_turn_streams_the_reply_straight_out_to_tg(tmp_path, monkeypatch):
    import synapse_tg.loop as mod
    monkeypatch.setattr(mod, "split_for_tg_typed",
                        lambda t: [{"kind": "text", "text": t}])
    monkeypatch.setattr(mod, "gfm_to_tg_html", lambda t: t)
    loop = TgLoop(_cfg(tmp_path))
    bot = FakeBot()
    loop._bot = bot
    loop._pending_chat_id = 5
    loop._provider = _FeedProvider()
    assert asyncio.run(loop.feed_turn("⏳ [NEW ROUND]\nnote")) is True
    assert loop._provider.sent == ["⏳ [NEW ROUND]\nnote"]
    assert [m["text"] for m in bot.sent] == ["free round reply"]


def test_feed_turn_without_a_chat_target_is_skipped(tmp_path):
    loop = TgLoop(_cfg(tmp_path))
    assert asyncio.run(loop.feed_turn("x")) is False


# ── enable switch ─────────────────────────────────────────────────────────────

def test_shell_disabled_injects_no_cortex_env(tmp_path):
    loop = TgLoop(TgConfig(data_dir=tmp_path / "d"))
    assert loop._make_provider().extra_env == {}
    assert loop._shell is None


def test_shell_enabled_injects_the_shell_id(tmp_path):
    loop = TgLoop(_cfg(tmp_path))
    assert loop._make_provider().extra_env == {"MARROW_CORTEX": "tg"}


def test_shell_after_turn_is_a_noop_without_a_host(tmp_path):
    loop = TgLoop(TgConfig(data_dir=tmp_path / "d"))
    asyncio.run(loop._shell_after_turn())        # must not raise


def test_config_defaults_shell_off_and_parses_the_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("")
    assert load_config(p).shell_enabled is False
    p.write_text(
        '[cortex]\n'
        'shell_enabled = true\n'
        'shell_id = "tg"\n'
        'shell_idle_min = 5\n'
        'note_render_cmd = ["py", "-m", "cortex.note_render"]\n'
        'fuse_tokens = 1234\n'
    )
    cfg = load_config(p)
    assert cfg.shell_enabled is True
    assert cfg.shell_idle_min == 5.0
    assert cfg.shell_note_render_cmd == ["py", "-m", "cortex.note_render"]
    assert cfg.shell_fuse_tokens == 1234


# ── state file protocol ───────────────────────────────────────────────────────

def test_state_write_merges_and_preserves_foreign_keys(tmp_path):
    d = tmp_path / "shells"
    shell_state.write(d, "tg", {"session_id": "a", "next_wake_at": "t1"})
    (d / "tg.json").write_text(json.dumps(
        {**json.loads((d / "tg.json").read_text()), "written_by_marrow": 1}))
    shell_state.write(d, "tg", {"next_wake_at": None, "occupancy": 9})
    assert shell_state.read(d, "tg") == {
        "session_id": "a", "occupancy": 9, "written_by_marrow": 1}
    assert (d / "tg.lock").exists()
    assert not list(d.glob("*.tmp.*"))


def test_state_read_missing_file(tmp_path):
    assert shell_state.read(tmp_path / "nope", "tg") == {}
