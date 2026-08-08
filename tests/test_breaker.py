"""Circuit breaker ("main switch") — bridge side.

The state file / tally / auto-trip protocol, plus the tg shell-host choke
points. Every real boundary is mocked: no claude spawn, no telegram bot, no
cortex subprocess, no live ~/.config/marrow writes (conftest guards that).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from synapse_core import breaker
from synapse_tg.config import TgConfig

from .test_tg_shell import (  # noqa: F401
    Clock, FakeBot, MIN, _cfg, _host, _NoSpawnProvider, _StubTyping,
)


@pytest.fixture(autouse=True)
def stub_boundaries(monkeypatch):
    """Same boundary stubs test_tg_shell installs (autouse fixtures do not
    cross modules): no cc spawn, no telegram typing action."""
    monkeypatch.setattr("synapse_tg.loop.ClaudeCodeProvider", _NoSpawnProvider)
    monkeypatch.setattr("synapse_tg.loop.TypingAction", _StubTyping)


@pytest.fixture
def cdir(tmp_path):
    return tmp_path


def _marrow_config(cdir, body: str) -> None:
    (cdir / "config.toml").write_text(body, encoding="utf-8")


# --- state file ---------------------------------------------------------------

def test_absent_file_is_clear(cdir):
    assert breaker.read(cdir) is None
    assert breaker.covers(cdir, "tg") is False


def test_trip_and_clear_roundtrip(cdir):
    st = breaker.trip(cdir, "all", breaker.REASON_MANUAL)
    assert st["scope"] == "all" and st["reason"] == "manual"
    on_disk = json.loads(breaker.breaker_path(cdir).read_text())
    assert set(on_disk) == {"scope", "reason", "ts"}
    assert datetime.fromisoformat(on_disk["ts"])
    assert breaker.covers(cdir, "tg") and breaker.covers(cdir, "cli")
    assert breaker.clear(cdir) is True
    assert breaker.clear(cdir) is False


def test_scope_is_per_shell(cdir):
    breaker.trip(cdir, "cli")
    assert breaker.covers(cdir, "cli") is True
    assert breaker.covers(cdir, "tg") is False


def test_corrupt_file_reads_as_clear(cdir, caplog):
    breaker.breaker_path(cdir).write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert breaker.read(cdir) is None
    assert "unreadable" in caplog.text


def test_write_leaves_no_tmp_file(cdir):
    breaker.trip(cdir, "all")
    assert [p.name for p in cdir.iterdir() if ".tmp." in p.name] == []


# --- duty hold union ------------------------------------------------------

def _write_duty(cdir, hold) -> None:
    breaker.duty_path(cdir).write_text(
        json.dumps({"mode": "tg", "hold": hold, "ts": datetime.now().astimezone().isoformat()}),
        encoding="utf-8")


def test_duty_path_is_sibling_of_breaker_path(cdir):
    assert breaker.duty_path(cdir) == breaker.breaker_path(cdir).with_name("duty.json")


def test_absent_duty_file_holds_nothing(cdir):
    assert breaker.duty_hold(cdir) is None
    assert breaker.covers(cdir, "tg") is False


def test_corrupt_duty_file_holds_nothing(cdir, caplog):
    breaker.duty_path(cdir).write_text("{not json", encoding="utf-8")
    assert breaker.duty_hold(cdir) is None
    assert breaker.covers(cdir, "tg") is False


def test_covers_true_via_duty_hold_alone(cdir):
    _write_duty(cdir, "tg")
    assert breaker.covers(cdir, "tg") is True
    assert breaker.covers(cdir, "cli") is False


def test_covers_true_via_manual_breaker_alone(cdir):
    breaker.trip(cdir, "tg")
    assert breaker.covers(cdir, "tg") is True
    assert breaker.duty_hold(cdir) is None


def test_covers_true_via_both_manual_and_duty(cdir):
    breaker.trip(cdir, "cli")
    _write_duty(cdir, "tg")
    assert breaker.covers(cdir, "cli") is True
    assert breaker.covers(cdir, "tg") is True


def test_duty_hold_all_covers_every_shell(cdir):
    _write_duty(cdir, "all")
    assert breaker.covers(cdir, "cli") is True
    assert breaker.covers(cdir, "tg") is True


def test_duty_hold_null_holds_nothing(cdir):
    _write_duty(cdir, None)
    assert breaker.duty_hold(cdir) is None
    assert breaker.covers(cdir, "tg") is False


# --- settings + tally ---------------------------------------------------------

def test_settings_read_from_marrow_config(cdir):
    _marrow_config(cdir, "[cortex.breaker]\nfuse_threshold = 4\nenabled = false\n")
    s = breaker.settings(cdir)
    assert s["fuse_threshold"] == 4 and s["enabled"] is False
    assert s["window_hours"] == 24  # unset -> default


def test_tally_counts_across_shells_and_prunes(cdir):
    _marrow_config(cdir, "[cortex.breaker]\nenabled = false\nwindow_hours = 24\n")
    now = datetime.now().astimezone()
    breaker.record_fuse(cdir, "cli", now=now - timedelta(hours=30))  # stale
    assert breaker.record_fuse(cdir, "tg", now=now) == 1
    assert breaker.record_fuse(cdir, "cli", now=now) == 2


def test_threshold_trips_for_all_shells(cdir):
    breaker.record_fuse_and_maybe_trip(cdir, "cli")
    count, tripped = breaker.record_fuse_and_maybe_trip(cdir, "tg")
    assert count == 2
    assert tripped["scope"] == "all" and tripped["reason"] == "auto_fuse"


def test_disabled_tallies_but_never_trips(cdir):
    _marrow_config(cdir, "[cortex.breaker]\nenabled = false\n")
    breaker.record_fuse_and_maybe_trip(cdir, "cli")
    count, tripped = breaker.record_fuse_and_maybe_trip(cdir, "tg")
    assert count == 2 and tripped is None
    assert breaker.read(cdir) is None


# --- tg choke point: no autonomous round while held ---------------------------

def test_held_breaker_skips_the_silence_round(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    breaker.trip(tmp_path, "tg")

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert fed == []


def test_held_breaker_leaves_the_wake_ledger_intact(tmp_path):
    """The booked wake is NOT consumed by the hold — it fires on the first
    round after the breaker clears."""
    from synapse_core import shell_state
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    due = datetime.fromtimestamp(clock.t).astimezone().isoformat()
    shell_state.write(tmp_path / "shells", "tg", {"next_wake_at": due})
    breaker.trip(tmp_path, "all")

    async def run():
        clock.t += 1 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert fed == []
    assert shell_state.read(tmp_path / "shells", "tg")["next_wake_at"] == due

    # Clear -> the same deadline now delivers.
    breaker.clear(tmp_path)
    asyncio.run(host._fire("tg"))
    assert len(fed) == 1
    assert shell_state.read(tmp_path / "shells", "tg").get("next_wake_at") is None


def test_held_breaker_skips_a_pending_direction(tmp_path):
    from synapse_core import shell_state
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    shell_state.write(tmp_path / "shells", "tg", {"pending_note": "go read"})
    breaker.trip(tmp_path, "tg")
    asyncio.run(host._fire("tg"))
    assert fed == []
    # The direction is still claimable once the breaker clears.
    assert shell_state.read(tmp_path / "shells", "tg")["pending_note"] == "go read"


def test_held_breaker_does_not_spin_the_scheduler(tmp_path):
    """A past-due deadline under a hold must re-arm into the FUTURE, else the
    scheduler refires it in a tight loop."""
    clock = Clock()
    host, _loop, _fed = _host(tmp_path, clock)
    breaker.trip(tmp_path, "all")
    armed: list[float] = []
    host._scheduler.schedule = lambda shell, at, cb: armed.append(at)
    clock.t += 60 * MIN
    asyncio.run(host._fire("tg"))
    assert armed and armed[-1] > clock.t


def test_breaker_covering_only_cli_leaves_tg_running(tmp_path):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    breaker.trip(tmp_path, "cli")

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    asyncio.run(run())
    assert len(fed) == 1


def test_unreadable_breaker_does_not_wedge_the_round(tmp_path, caplog):
    clock = Clock()
    host, _loop, fed = _host(tmp_path, clock)
    breaker.breaker_path(tmp_path).write_text("{broken", encoding="utf-8")

    async def run():
        host._arm()
        clock.t += 20 * MIN
        await host._fire("tg")

    with caplog.at_level("WARNING"):
        asyncio.run(run())
    assert len(fed) == 1  # corrupt = clear, the shell keeps working


# --- tg fuse: tally, trip, announce -------------------------------------------

def test_tg_fuse_records_an_event(tmp_path):
    _marrow_config(tmp_path, "[cortex.breaker]\nenabled = false\n")
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=100)
    loop._state.last_assistant_usage = {"input_tokens": 150}
    asyncio.run(host.after_turn())
    events = json.loads(breaker.fuse_path(tmp_path).read_text())["events"]
    assert [e["shell"] for e in events] == ["tg"]
    # Still feeds the wrap-up prompt + respawns: nothing tripped.
    assert fed[0].startswith("⚙️ [FUSE]") and fed[1] == "__RESPAWN__"


def test_tg_fuse_trip_writes_alert_and_sends_a_notice(tmp_path):
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=100)
    bot = FakeBot()
    loop._outbound_target = lambda: (bot, 42)
    written: list[tuple] = []
    loop._alerts = type("S", (), {
        "write": lambda self, *a, **k: written.append((a, k))})()

    breaker.record_fuse(tmp_path, "cli")  # 1st fuse, other shell
    loop._state.last_assistant_usage = {"input_tokens": 150}
    asyncio.run(host.after_turn())        # 2nd -> trip

    st = breaker.read(tmp_path)
    assert st["scope"] == "all" and st["reason"] == "auto_fuse"
    assert written and written[0][0][0] == "critical"
    assert written[0][0][1] == "cortex_breaker_tripped"
    assert len(bot.sent) == 1
    assert "Circuit breaker tripped" in bot.sent[0]["text"]
    # The wrap-up prompt is skipped (autonomous feed under a fresh hold) but the
    # oversized session is still dropped.
    assert fed == ["__RESPAWN__"]


def test_tg_fuse_trip_without_a_bot_still_holds(tmp_path):
    clock = Clock()
    host, loop, _fed = _host(tmp_path, clock, shell_fuse_tokens=100)
    loop._outbound_target = lambda: (None, None)
    breaker.record_fuse(tmp_path, "cli")
    loop._state.last_assistant_usage = {"input_tokens": 150}
    asyncio.run(host.after_turn())
    assert breaker.read(tmp_path)["reason"] == "auto_fuse"


def test_tg_fuse_disabled_config_never_trips(tmp_path):
    _marrow_config(tmp_path, "[cortex.breaker]\nenabled = false\nfuse_threshold = 2\n")
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_fuse_tokens=100)
    breaker.record_fuse(tmp_path, "cli")
    loop._state.last_assistant_usage = {"input_tokens": 150}
    asyncio.run(host.after_turn())
    assert breaker.read(tmp_path) is None
    assert fed[0].startswith("⚙️ [FUSE]")


def test_marrow_config_dir_is_the_marrow_db_parent(tmp_path):
    cfg = TgConfig(marrow_db=str(tmp_path / "sub" / "marrow.db"))
    assert cfg.marrow_config_dir() == tmp_path / "sub"


# --- announcing a trip written by the OTHER shell -----------------------------
# The cli watchdog writes breaker.json + a marrow alert row and has no channel
# of its own, so the notice reaches her from this bridge's checkpoints.

def _announcing_host(tmp_path, **kw):
    """Host whose outbound target is a FakeBot, ready to announce."""
    host, loop, fed = _host(tmp_path, Clock(), **kw)
    bot = FakeBot()
    loop._outbound_target = lambda: (bot, 42)
    return host, loop, bot, fed


def _cli_trip(tmp_path, *, count: int = 2):
    """A cli-origin auto trip: the tally the message quotes, then the state
    file the watchdog would have written."""
    for _ in range(count):
        breaker.record_fuse(tmp_path, "cli")
    return breaker.trip(tmp_path, breaker.SCOPE_ALL, breaker.REASON_AUTO)


def test_cli_trip_is_announced_once_then_never_again(tmp_path):
    host, _loop, bot, _fed = _announcing_host(tmp_path)
    _cli_trip(tmp_path)

    asyncio.run(host._announce_breaker_trip())
    assert len(bot.sent) == 1
    assert "Circuit breaker tripped" in bot.sent[0]["text"]
    assert "#2" in bot.sent[0]["text"]  # count read from the live tally

    # Every later pass (idle round, after_turn) sees the same trip -> silent.
    asyncio.run(host._announce_breaker_trip())
    asyncio.run(host.after_turn())
    assert len(bot.sent) == 1


def test_cleared_then_retripped_announces_again(tmp_path):
    host, _loop, bot, _fed = _announcing_host(tmp_path)
    _cli_trip(tmp_path)
    asyncio.run(host._announce_breaker_trip())

    breaker.clear(tmp_path)
    asyncio.run(host._announce_breaker_trip())   # clear itself says nothing
    assert len(bot.sent) == 1

    breaker.trip(tmp_path, breaker.SCOPE_ALL, breaker.REASON_AUTO,
                 now=datetime.now().astimezone() + timedelta(minutes=5))
    asyncio.run(host._announce_breaker_trip())
    assert len(bot.sent) == 2                    # a NEW trip is a new notice


def test_manual_pause_is_never_announced(tmp_path):
    """A manual pause is her own doing, and trip_message describes a fuse
    breach — announcing it would be a lie about why the shell stopped."""
    host, _loop, bot, _fed = _announcing_host(tmp_path)
    breaker.trip(tmp_path, breaker.SCOPE_ALL, breaker.REASON_MANUAL)
    asyncio.run(host._announce_breaker_trip())
    assert bot.sent == []


def test_tg_own_trip_is_not_announced_twice(tmp_path):
    """_record_fuse sends the notice itself and stamps the marker, so the
    checkpoint that runs right after it in after_turn stays silent."""
    host, loop, bot, fed = _announcing_host(tmp_path, shell_fuse_tokens=100)
    breaker.record_fuse(tmp_path, "cli")            # 1st fuse, other shell
    loop._state.last_assistant_usage = {"input_tokens": 150}

    asyncio.run(host.after_turn())                  # 2nd -> tg trips
    assert breaker.read(tmp_path)["reason"] == "auto_fuse"
    assert len(bot.sent) == 1                       # exactly one notice

    asyncio.run(host._announce_breaker_trip())      # next checkpoint
    asyncio.run(host.after_turn())
    assert len(bot.sent) == 1
    assert fed == ["__RESPAWN__"]


def test_marker_survives_a_restart(tmp_path):
    """The marker lives in the shell ledger, so a fresh ShellHost over the same
    state dir does not re-announce a trip the previous process already sent."""
    host, _loop, bot, _fed = _announcing_host(tmp_path)
    _cli_trip(tmp_path)
    asyncio.run(host._announce_breaker_trip())
    assert len(bot.sent) == 1

    host2, _loop2, bot2, _fed2 = _announcing_host(tmp_path)
    asyncio.run(host2._announce_breaker_trip())
    assert bot2.sent == []


def test_first_boot_adopts_a_standing_trip_without_announcing(tmp_path):
    """Marker init: a trip that predates this bridge's first ever boot is
    adopted silently — she was told about it by whatever was running then."""
    _cli_trip(tmp_path)
    host, _loop, bot, _fed = _announcing_host(tmp_path)   # constructs the marker
    asyncio.run(host._announce_breaker_trip())
    assert bot.sent == []


def test_trip_written_while_the_bridge_was_down_is_announced_on_boot(tmp_path):
    """The opposite case: once the marker exists, a trip written while this
    process was down is NOT adopted — it is announced on the first pass."""
    host, _loop, bot, _fed = _announcing_host(tmp_path)   # marker inits clear
    assert bot.sent == []
    _cli_trip(tmp_path)                                   # trip while "down"

    host2, _loop2, bot2, _fed2 = _announcing_host(tmp_path)
    asyncio.run(host2._announce_breaker_trip())
    assert len(bot2.sent) == 1


def test_unreadable_breaker_file_neither_crashes_nor_announces(tmp_path, caplog):
    host, _loop, bot, _fed = _announcing_host(tmp_path)
    breaker.breaker_path(tmp_path).write_text("{not json", encoding="utf-8")
    asyncio.run(host._announce_breaker_trip())   # must not raise
    assert bot.sent == []
    asyncio.run(host.after_turn())               # nor on the fast checkpoint
    assert bot.sent == []


def test_announce_failure_does_not_re_announce_forever(tmp_path):
    """The marker is stamped before the send: a bot that is down costs that one
    notice, it does not queue a repeat on every later turn."""
    host, loop, _bot, _fed = _announcing_host(tmp_path)

    class _DeadBot:
        async def send_message(self, **_):
            raise RuntimeError("telegram down")

    loop._outbound_target = lambda: (_DeadBot(), 42)
    _cli_trip(tmp_path)
    asyncio.run(host._announce_breaker_trip())   # swallowed by _notify

    bot = FakeBot()
    loop._outbound_target = lambda: (bot, 42)
    asyncio.run(host._announce_breaker_trip())
    assert bot.sent == []


def test_idle_round_announces_on_the_silent_path(tmp_path):
    """No turns coming: the scheduled round is the announce point, and the
    breaker still holds the round itself."""
    clock = Clock()
    host, loop, fed = _host(tmp_path, clock, shell_idle_min=20.0)
    bot = FakeBot()
    loop._outbound_target = lambda: (bot, 42)
    _cli_trip(tmp_path)

    clock.t += 21 * MIN
    asyncio.run(host._fire("tg"))
    assert len(bot.sent) == 1
    assert fed == []            # held: announced, but no autonomous round


def test_fuse_count_reads_without_recording(tmp_path):
    breaker.record_fuse(tmp_path, "cli")
    breaker.record_fuse(tmp_path, "tg")
    assert breaker.fuse_count(tmp_path) == 2
    assert breaker.fuse_count(tmp_path) == 2      # read-only, no tally growth


def test_fuse_count_ignores_events_outside_the_window(tmp_path):
    _marrow_config(tmp_path, "[cortex.breaker]\nwindow_hours = 1\n")
    old = (datetime.now().astimezone() - timedelta(hours=3)).isoformat()
    breaker.fuse_path(tmp_path).write_text(
        json.dumps({"events": [{"ts": old, "shell": "cli"}]}), encoding="utf-8")
    assert breaker.fuse_count(tmp_path) == 0


def test_fuse_count_on_an_absent_tally_is_zero(tmp_path):
    assert breaker.fuse_count(tmp_path) == 0
