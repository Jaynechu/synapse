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


class _NoSpawnProvider:
    """Stands in for ClaudeCodeProvider at the spawn boundary: keeps every
    constructor kwarg readable, never starts a cc process."""

    alive = True
    session_id = None
    turn_output_capped = False

    def __init__(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.extra_env = kwargs.get("extra_env") or {}   # cc.py:162

    def is_alive(self):
        return True

    def spawn(self):
        pass

    def cancel(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def stub_provider(monkeypatch):
    """No cc spawn anywhere in this module, so the real _make_provider /
    _swap_provider / shell_respawn code paths can run."""
    monkeypatch.setattr("synapse_tg.loop.ClaudeCodeProvider", _NoSpawnProvider)


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
    # Marrow config dir = parent of marrow_db (shared with breaker files).
    # shell_active() reads [cortex].shells from here (T7 single source) —
    # write it so ShellHost tests see the shell as active by default.
    marrow_cfg = tmp_path / "config.toml"
    if not marrow_cfg.exists():
        marrow_cfg.write_text('[cortex]\nshells = ["tg"]\n')
    base = dict(
        data_dir=tmp_path / "tg-data",
        marrow_db=str(tmp_path / "marrow.db"),
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
    real_respawn = loop.shell_respawn

    def _respawn():                     # records, then runs the real thing
        fed.append("__RESPAWN__")
        real_respawn()

    loop.shell_respawn = _respawn
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


# ── render failure alert ──────────────────────────────────────────────────────

class _RecordingAlerts:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, severity, kind, message, source="", *, fingerprint=None):
        self.rows.append({"severity": severity, "kind": kind, "message": message,
                          "source": source, "fingerprint": fingerprint})


class _FakeRun:
    """Stands in for subprocess.run at the render boundary — no real spawn."""

    def __init__(self, rc=1, out="") -> None:
        self.rc = rc
        self.out = out
        self.calls = 0

    def __call__(self, *_a, **_kw):
        self.calls += 1
        import subprocess as sp
        return sp.CompletedProcess([], self.rc, stdout=self.out, stderr="boom")


@pytest.fixture
def fake_run(monkeypatch):
    runner = _FakeRun()
    monkeypatch.setattr("synapse_tg.shell.subprocess.run", runner)
    return runner


def _alerting_host(tmp_path, **kw):
    cfg = _cfg(tmp_path, **kw)
    loop = TgLoop(cfg)
    alerts = _RecordingAlerts()
    loop._alerts = alerts
    return ShellHost(cfg, loop, clock=Clock()), alerts


def test_render_failures_alert_once_at_the_threshold(tmp_path, fake_run):
    """A renderer that keeps failing mutes every autonomous round — the third
    failure raises exactly one alert, not one per round."""
    host, alerts = _alerting_host(tmp_path)
    for _ in range(5):
        assert host._render_note() is None
    assert len(alerts.rows) == 1
    row = alerts.rows[0]
    assert row["severity"] == "warn"
    assert row["kind"] == "shell_note_render_failed"
    assert row["fingerprint"] == "shell_note_render_failed"
    assert "3x" in row["message"]


def test_render_failures_below_the_threshold_do_not_alert(tmp_path, fake_run):
    host, alerts = _alerting_host(tmp_path)
    host._render_note()
    host._render_note()
    assert alerts.rows == []


def test_a_successful_render_resets_the_failure_streak(tmp_path, fake_run):
    """Two failures, a good round, two more failures -> still silent; the
    streak restarts, so a flaky renderer never trips the alert."""
    host, alerts = _alerting_host(tmp_path)
    host._render_note()
    host._render_note()
    fake_run.rc = 0                                 # exit 0, empty note
    assert host._render_note() is None
    fake_run.rc = 1
    host._render_note()
    host._render_note()
    assert alerts.rows == []
    host._render_note()
    assert len(alerts.rows) == 1


def test_render_spawn_error_counts_towards_the_alert(tmp_path, monkeypatch):
    def _boom(*_a, **_kw):
        raise OSError("no such renderer")

    monkeypatch.setattr("synapse_tg.shell.subprocess.run", _boom)
    host, alerts = _alerting_host(tmp_path)
    for _ in range(3):
        assert host._render_note() is None
    assert len(alerts.rows) == 1
    assert "no such renderer" in alerts.rows[0]["message"]


def test_missing_render_cmd_also_counts_towards_the_alert(tmp_path):
    host, alerts = _alerting_host(tmp_path, shell_note_render_cmd=[])
    for _ in range(3):
        host._render_note()
    assert len(alerts.rows) == 1
    assert "note_render_cmd unset" in alerts.rows[0]["message"]


def test_render_alert_can_be_switched_off(tmp_path, fake_run):
    host, alerts = _alerting_host(tmp_path, shell_note_render_alert_after=0)
    for _ in range(5):
        host._render_note()
    assert alerts.rows == []


def test_render_failure_without_an_alert_sink_still_skips_cleanly(tmp_path, fake_run):
    cfg = _cfg(tmp_path)
    host = ShellHost(cfg, TgLoop(cfg), clock=Clock())   # loop._alerts is None
    for _ in range(4):
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


# ── wake vs idle: mutual exclusion, persisted basis ───────────────────────────

def _iso_utc(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _basis(tmp_path) -> float | None:
    return parse_wake_at(shell_state.read(tmp_path / "shells", "tg").get("last_user_ts"))


def test_a_booked_wake_suspends_the_idle_cycle(tmp_path):
    """Asleep until +3h: 25 silent minutes must NOT feed an idle round — the
    booked wake owns the deadline until it is spent."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    wake_at = clock.t + 180 * MIN
    shell_state.write(tmp_path / "shells", "tg", {"next_wake_at": _iso_utc(wake_at)})

    async def run():
        host._arm()
        assert host._scheduler._table["tg"][0] == pytest.approx(wake_at, abs=1)
        clock.t += 25 * MIN                  # past the idle window
        await host._fire("tg")               # a kick lands while asleep
        assert fed == []
        assert host._scheduler._table["tg"][0] == pytest.approx(wake_at, abs=1)
        clock.t = wake_at
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 1
    assert "next_wake_at" not in shell_state.read(tmp_path / "shells", "tg")


def test_user_message_cancels_the_booked_wake_and_restarts_the_window(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    shell_state.write(tmp_path / "shells", "tg",
                      {"next_wake_at": _iso_utc(clock.t + 180 * MIN)})

    async def run():
        host._arm()
        clock.t += 5 * MIN
        host.on_user_message()

    asyncio.run(run())
    assert fed == []
    assert "next_wake_at" not in shell_state.read(tmp_path / "shells", "tg")
    assert _basis(tmp_path) == pytest.approx(clock.t, abs=1)
    assert host._scheduler._table["tg"][0] == pytest.approx(clock.t + 20 * MIN, abs=1)


def test_restart_continues_the_running_idle_window(tmp_path):
    """Basis 5min old on disk -> the fresh host owes 15 more minutes, and the
    boot pass itself feeds nothing."""
    clock = Clock()
    shell_state.write(tmp_path / "shells", "tg",
                      {"last_user_ts": _iso_utc(clock.t - 5 * MIN)})
    host, _loop, fed = _host(tmp_path, clock)

    async def run():
        host._arm()
        assert host._scheduler._table["tg"][0] == pytest.approx(clock.t + 15 * MIN, abs=1)
        await host._fire("tg")               # kick right after boot
        assert fed == []
        clock.t += 15 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 1


@pytest.mark.parametrize("ledger", [
    {"last_user_ts": _iso_utc(1_000_000.0 - 40 * MIN)},   # elapsed
    {},                                                    # missing
    {"last_user_ts": "not a timestamp"},                   # corrupt
    {"last_user_ts": 12345},                               # wrong type
])
def test_restart_with_an_unusable_basis_opens_a_full_fresh_window(tmp_path, ledger):
    """A window being reopened never delivers a note by itself: the basis
    becomes now, so the first fire is a whole idle window away."""
    clock = Clock()
    if ledger:
        shell_state.write(tmp_path / "shells", "tg", ledger)
    host, _loop, fed = _host(tmp_path, clock)

    async def run():
        host._arm()
        assert host._scheduler._table["tg"][0] == pytest.approx(clock.t + 20 * MIN, abs=1)
        await host._fire("tg")               # boot pass fires nothing
        assert fed == []
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 1


def test_an_interrupted_feed_still_costs_its_whole_window(tmp_path):
    """The idle basis is banked before delivery, so a feed that dies mid-flight
    is not retried and cannot re-fire on the next pass — nor after a restart."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock)

    async def _boom(body):
        fed.append(body)
        raise asyncio.CancelledError

    async def _ok(body):
        fed.append(body)
        return True

    loop.feed_turn = _boom

    async def run():
        host._arm()
        clock.t += 20 * MIN
        with pytest.raises(asyncio.CancelledError):
            await host._fire("tg")
        fire_ts = clock.t
        assert _basis(tmp_path) == pytest.approx(fire_ts, abs=1)

        loop.feed_turn = _ok
        clock.t += 19 * MIN
        await host._fire("tg")               # next pass: still inside the window
        assert len(fed) == 1
        # a restart in the same window owes only the remaining minute
        restarted, _l, _f = _host(tmp_path, clock)
        assert restarted._silence_deadline() == pytest.approx(fire_ts + 20 * MIN, abs=1)

        clock.t += 1 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 2


def test_fire_exception_still_rearms_the_shell(tmp_path, caplog):
    """A callback exception must never leave the shell with no deadline: the
    scheduler pops the entry before calling in, so a swallowed exception
    without a re-arm would mean no idle fire and no wake fire until an
    external kick landed."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock)

    async def _boom(body):
        raise RuntimeError("feed exploded")

    loop.feed_turn = _boom

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")  # must not raise

    with caplog.at_level("ERROR"):
        asyncio.run(run())

    assert fed == []
    assert "tg" in host._scheduler._table
    at, _cb = host._scheduler._table["tg"]
    assert at == pytest.approx(clock.t + 20 * MIN, abs=1)
    assert "shell fire failed for tg" in caplog.text


def test_fire_normal_path_arms_exactly_once(tmp_path):
    """The exception net must not change the normal path: no double arm."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    calls: list[float] = []
    real_arm = host._arm

    def _counted_arm(state=None):
        calls.append(clock.t)
        return real_arm(state)

    host._arm = _counted_arm

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 1
    assert len(calls) == 2  # the boot arm + the one at the end of _fire


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
    assert st["tokens_today_base"] == 0 and st["tokens_date"] == host._local_date()
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


# ── today's token ledger (tokens_today_base / tokens_date) ────────────────────

def test_respawn_folds_the_closing_occupancy_into_todays_base(tmp_path):
    """Two fuse respawns: today's base accumulates both sessions' finals, so
    cortex renders today = base + live occupancy."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=100)
    d = tmp_path / "shells"
    for occ in (150, 120):
        loop._state.last_assistant_usage = {"input_tokens": occ}
        asyncio.run(host.after_turn())
    st = shell_state.read(d, "tg")
    assert st["tokens_today_base"] == 270 and st["occupancy"] == 0
    assert st["tokens_date"] == host._local_date()


def test_date_rollover_resets_the_base(tmp_path):
    """A base stamped with a past local date is yesterday's — it never adds to
    today, neither on a plain turn nor on the fold."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=100)
    d = tmp_path / "shells"
    shell_state.write(d, "tg", {"tokens_today_base": 500_000,
                                "tokens_date": "1999-01-01"})
    loop._state.last_assistant_usage = {"input_tokens": 10}
    asyncio.run(host.after_turn())                     # under fuse: plain write
    st = shell_state.read(d, "tg")
    assert st["tokens_today_base"] == 0 and st["tokens_date"] == host._local_date()

    loop._state.last_assistant_usage = {"input_tokens": 150}
    asyncio.run(host.after_turn())                     # fuse: fold onto 0
    assert shell_state.read(d, "tg")["tokens_today_base"] == 150


def test_local_date_follows_the_configured_timezone(tmp_path):
    """Same instant, two zones, two dates — the date comes from
    [core].timezone, not the host machine."""
    ts = 1_767_222_000.0                               # 2026-01-01 03:00 UTC
    mel = ShellHost(_cfg(tmp_path, timezone="Australia/Melbourne"),
                    TgLoop(_cfg(tmp_path)), clock=Clock(ts))
    utc = ShellHost(_cfg(tmp_path, timezone="UTC"),
                    TgLoop(_cfg(tmp_path)), clock=Clock(ts))
    assert mel._local_date() == "2026-01-01"
    assert utc._local_date() == "2025-12-31"


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


# ── every window end folds, not just the fuse ─────────────────────────────────

def _turn(host, loop, occ, sid="sess-1"):
    loop._state.session_id = sid
    loop._state.last_assistant_usage = {"input_tokens": occ}
    asyncio.run(host.after_turn())


def test_manual_session_reset_folds_the_closing_occupancy(tmp_path):
    """/clear, /cwd and the cross-channel takeover all end the window through
    the registry's swap_provider — the tokens they burnt still count today."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=0)
    _turn(host, loop, 150)
    assert shell_state.read(tmp_path / "shells", "tg")["occupancy"] == 150

    loop._state.session_id = None                    # what the registry does
    loop._registry._ctx.swap_provider("opus", None)  # the real reset path

    st = shell_state.read(tmp_path / "shells", "tg")
    assert st["tokens_today_base"] == 150 and st["occupancy"] == 0
    assert fed == []                                 # no fuse prompt


def test_resume_of_the_same_session_is_not_a_window_end(tmp_path):
    """/model respawns cc on the SAME sid — one window continuing, so folding
    it would count that context twice."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=0)
    _turn(host, loop, 150, sid="sess-1")

    loop._registry._ctx.swap_provider("sonnet", "sess-1")

    st = shell_state.read(tmp_path / "shells", "tg")
    assert st.get("tokens_today_base", 0) == 0 and st["occupancy"] == 150


def test_resume_of_a_different_session_folds(tmp_path):
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=0)
    _turn(host, loop, 150, sid="sess-1")

    loop._registry._ctx.swap_provider("opus", "sess-2")

    assert shell_state.read(tmp_path / "shells", "tg")["tokens_today_base"] == 150


def test_fold_is_idempotent_when_two_paths_overlap(tmp_path):
    """Two termination paths on one window (e.g. close then swap) must not
    double-count: the fold zeroes the occupancy it consumed."""
    clock = Clock()
    host, loop, _fed = _host(tmp_path, clock, shell_fuse_tokens=0)
    _turn(host, loop, 150)

    host.fold_session(None)
    host.fold_session(None)

    st = shell_state.read(tmp_path / "shells", "tg")
    assert st["tokens_today_base"] == 150 and st["occupancy"] == 0


def test_crash_respawn_resumes_the_same_window_without_folding(tmp_path):
    clock = Clock()
    host, loop, _fed = _host(tmp_path, clock, shell_fuse_tokens=0)
    _turn(host, loop, 150, sid="sess-1")

    loop._respawn()                                  # provider died mid-turn

    st = shell_state.read(tmp_path / "shells", "tg")
    assert st.get("tokens_today_base", 0) == 0 and st["occupancy"] == 150


def test_plain_relay_resident_never_touches_a_ledger(tmp_path):
    loop = TgLoop(_cfg(tmp_path))                    # no attach_shell
    loop._make_provider()
    assert not (tmp_path / "shells").exists()


# ── lie_down(rotate=True) from the shell ──────────────────────────────────────

def test_rotate_pending_respawns_without_feeding_anything(tmp_path):
    """marrow flags rotate_pending + kicks: the round ends the window instead
    of feeding a turn — no note, no fuse prompt. The wake it booked stands."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock)
    d = tmp_path / "shells"
    _turn(host, loop, 150)
    loop._buffer.add("said mid-rotate")
    from datetime import datetime, timezone
    later = datetime.fromtimestamp(clock.t + 5 * MIN, timezone.utc).isoformat()
    shell_state.write(d, "tg", {"rotate_pending": True, "next_wake_at": later})

    asyncio.run(host._fire("tg"))

    assert fed == ["__RESPAWN__"]                    # no NOTE BODY, no FUSE
    st = shell_state.read(d, "tg")
    assert "rotate_pending" not in st                # claimed once
    assert st["next_wake_at"] == later               # the booked wake survives
    assert st["tokens_today_base"] == 150 and st["occupancy"] == 0
    assert loop._state.session_id is None
    assert loop._buffer.flush() == "said mid-rotate"  # rides into the new session
    assert host._scheduler._table["tg"][0] == pytest.approx(clock.t + 5 * MIN, abs=1)


def test_rotate_with_zero_minutes_wakes_the_fresh_session_at_once(tmp_path):
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock)
    d = tmp_path / "shells"
    from datetime import datetime, timezone
    now_iso = datetime.fromtimestamp(clock.t, timezone.utc).isoformat()
    shell_state.write(d, "tg", {"rotate_pending": True, "next_wake_at": now_iso})

    async def run():
        await host._fire("tg")       # rotate
        await host._fire("tg")       # the re-armed immediate wake

    asyncio.run(run())
    assert fed[0] == "__RESPAWN__"
    assert fed[1].startswith("⏳ [NEW ROUND]") and "NOTE BODY" in fed[1]


def test_rotate_without_a_booked_wake_opens_a_fresh_idle_window(tmp_path):
    """A rotate ends a spent window: the new session must get a whole
    shell_idle_min of silence, not a note on the very next tick."""
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    d = tmp_path / "shells"

    async def run():
        host._arm()
        clock.t += 20 * MIN                  # the window that led to the rotate
        shell_state.write(d, "tg", {"rotate_pending": True})
        await host._fire("tg")
        assert fed == ["__RESPAWN__"]
        rotated_at = clock.t
        assert _basis(tmp_path) == pytest.approx(rotated_at, abs=1)
        assert host._scheduler._table["tg"][0] == pytest.approx(
            rotated_at + 20 * MIN, abs=1)
        await host._fire("tg")               # next scheduler pass: nothing due
        assert fed == ["__RESPAWN__"]
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert fed[1].startswith("⏳ [NEW ROUND]")


def test_rotate_waits_for_an_in_flight_turn(tmp_path):
    """A rotate kick can land mid-turn; the respawn must not cut the stream."""
    cfg = _cfg(tmp_path)
    loop = TgLoop(cfg)
    loop._state.session_id = "old-sid"

    async def run():
        await loop._lock.acquire()                   # a turn is streaming
        task = asyncio.create_task(loop.shell_rotate())
        await asyncio.sleep(0)
        mid = loop._state.session_id
        loop._lock.release()
        await task
        return mid

    assert asyncio.run(run()) == "old-sid"           # untouched while locked
    assert loop._state.session_id is None


def _freeze_now(monkeypatch, loop):
    """Pin datetime.now() at 22:15 local; return that epoch."""
    import synapse_tg.loop as mod

    class _Fixed(mod.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 26, 22, 15, tzinfo=tz)

    monkeypatch.setattr(mod, "datetime", _Fixed)
    return _Fixed.now(loop._tz).timestamp()


def test_shell_rotate_notice_carries_the_booked_wake(tmp_path, monkeypatch):
    """The truthful rotation signal: shell_rotate() sends 🌙 after respawning,
    to the outbound target, announcing the wake the shell just booked."""
    cfg = _cfg(tmp_path, chat_id=4242)
    loop = TgLoop(cfg)
    now = _freeze_now(monkeypatch, loop)
    bot = FakeBot()
    loop.attach_bot(bot)

    asyncio.run(loop.shell_rotate(now + 30 * MIN))

    assert [m["text"] for m in bot.sent] == ["\U0001f319 Rotated 30min 22:45"]
    assert {m["chat_id"] for m in bot.sent} == {4242}


@pytest.mark.parametrize("wake", [None, -5 * MIN, 0.0])
def test_shell_rotate_notice_bare_without_a_future_wake(tmp_path, monkeypatch, wake):
    """No wake booked, or one already past: 🌙 alone, no misleading time."""
    cfg = _cfg(tmp_path, chat_id=4242)
    loop = TgLoop(cfg)
    now = _freeze_now(monkeypatch, loop)
    bot = FakeBot()
    loop.attach_bot(bot)

    asyncio.run(loop.shell_rotate(None if wake is None else now + wake))

    assert [m["text"] for m in bot.sent] == ["\U0001f319 Rotated"]


def test_rotate_fire_passes_the_ledger_wake_to_the_notice(tmp_path):
    """_fire() reads next_wake_at off the same state dict it claimed the
    rotate flag from, and hands it to shell_rotate()."""
    from datetime import datetime, timezone

    clock = Clock()
    host, loop, _ = _host(tmp_path, clock)
    seen: list[float | None] = []

    async def _rotate(wake=None):
        seen.append(wake)

    loop.shell_rotate = _rotate
    later = datetime.fromtimestamp(clock.t + 30 * MIN, timezone.utc).isoformat()
    shell_state.write(tmp_path / "shells", "tg",
                      {"rotate_pending": True, "next_wake_at": later})

    asyncio.run(host._fire("tg"))

    assert seen == [pytest.approx(clock.t + 30 * MIN, abs=1)]


def test_shell_rotate_sends_no_notice_without_a_chat_target(tmp_path):
    cfg = _cfg(tmp_path)
    loop = TgLoop(cfg)
    asyncio.run(loop.shell_rotate())  # no bot attached, no chat_id — no crash


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


# ── enable switch (T7: marrow [cortex].shells single source) ──────────────────

def test_shell_off_when_marrow_config_absent_injects_no_cortex_env(tmp_path):
    """Missing marrow config -> shell off (standalone-synapse fallback)."""
    loop = TgLoop(TgConfig(data_dir=tmp_path / "d", marrow_db=str(tmp_path / "marrow.db")))
    assert loop._make_provider().extra_env == {}
    assert loop._shell is None


def test_shell_off_when_tg_absent_from_marrow_shells(tmp_path):
    (tmp_path / "config.toml").write_text('[cortex]\nshells = ["cli"]\n')
    loop = TgLoop(TgConfig(data_dir=tmp_path / "d", marrow_db=str(tmp_path / "marrow.db")))
    assert loop._make_provider().extra_env == {}


def test_shell_active_injects_the_shell_id(tmp_path):
    loop = TgLoop(_cfg(tmp_path))
    assert loop._make_provider().extra_env == {"MARROW_CORTEX": "tg"}


def test_shell_after_turn_is_a_noop_without_a_host(tmp_path):
    loop = TgLoop(TgConfig(data_dir=tmp_path / "d"))
    asyncio.run(loop._shell_after_turn())        # must not raise


def test_config_parses_the_cortex_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[cortex]\n'
        'shell_id = "tg"\n'
        'shell_idle_min = 5\n'
        'note_render_cmd = ["py", "-m", "cortex.note_render"]\n'
        'note_render_alert_after = 5\n'
        'fuse_tokens = 1234\n'
    )
    cfg = load_config(p)
    assert cfg.shell_idle_min == 5.0
    assert cfg.shell_note_render_cmd == ["py", "-m", "cortex.note_render"]
    assert cfg.shell_note_render_alert_after == 5
    assert cfg.shell_fuse_tokens == 1234


def test_config_leftover_shell_enabled_key_warns_not_fatal(tmp_path, caplog):
    p = tmp_path / "config.toml"
    p.write_text('[cortex]\nshell_enabled = true\n')
    with caplog.at_level("WARNING"):
        cfg = load_config(p)
    assert any("shell_enabled" in r.message for r in caplog.records)
    assert not hasattr(cfg, "shell_enabled")


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


# ── production wiring: a restarted bridge has no inbound message yet ──────────
#
# The whole shell was dead in production because these paths were only ever
# tested with feed_turn stubbed out. Wired the way __main__._post_init wires
# them, a fired round must still reach tg through the configured [tg].chat_id.

class _ScriptedProvider:
    """Resident that answers one turn. Never spawns anything."""

    turn_output_capped = False

    def __init__(self, text="reply text", sid="sess-boot") -> None:
        self.alive = True
        self.session_id = sid
        self.sent: list[str] = []
        self._text = text

    def is_alive(self):
        return True

    def spawn(self):
        pass

    def send(self, body):
        self.sent.append(body)

    def recv(self, first_line=None):
        yield {"type": "system", "subtype": "init", "session_id": self.session_id}
        yield {"type": "assistant", "message": {
            "content": [{"type": "text", "text": self._text}],
            "usage": {"input_tokens": 11, "output_tokens": 2}}}
        yield {"type": "result", "result": self._text}

    def poll_line(self, timeout):
        return None

    def cancel(self):
        pass

    def close(self):
        pass


def _wired(tmp_path, clock, **kw):
    """host + loop + bot wired exactly as __main__._post_init does: the real
    feed_turn, the bot seeded from the application, no inbound message yet."""
    cfg = _cfg(tmp_path, chat_id=4242, **kw)
    loop = TgLoop(cfg)
    bot = FakeBot()
    loop.attach_bot(bot)                    # __main__.py:254
    host = ShellHost(cfg, loop, clock=clock)
    loop.attach_shell(host)                 # __main__.py:260
    host._render_note = lambda: "NOTE BODY"
    loop._provider = _ScriptedProvider()
    return host, loop, bot


def test_silence_round_reaches_tg_without_any_inbound_message(tmp_path):
    clock = Clock()
    host, loop, bot = _wired(tmp_path, clock)
    assert loop._pending_chat_id is None     # fresh boot: nothing inbound yet

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())

    assert loop._provider.sent and "NOTE BODY" in loop._provider.sent[0]
    assert bot.sent, "the fed round's reply must ship to the configured chat"
    assert {m["chat_id"] for m in bot.sent} == {4242}


def test_a_wired_turn_writes_the_ledger(tmp_path):
    clock = Clock()
    host, loop, _bot = _wired(tmp_path, clock)
    assert not shell_state.state_path(tmp_path / "shells", "tg").exists()

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())

    st = shell_state.read(tmp_path / "shells", "tg")
    assert st["session_id"] == "sess-boot"
    assert st["occupancy"] == 13             # 11 input + 2 output
    assert st["last_note_ts"]
    assert st["tokens_date"] == host._local_date()


def test_live_chat_still_wins_over_the_configured_chat_id(tmp_path):
    clock = Clock()
    host, loop, bot = _wired(tmp_path, clock)
    loop._pending_chat_id = 99               # an inbound message landed

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert {m["chat_id"] for m in bot.sent} == {99}
