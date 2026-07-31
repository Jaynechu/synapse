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
