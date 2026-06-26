"""Tests for IdleFireLoop — cross-channel cleanup and mid-session scan trigger."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from synapse_core.sessionend.idle import IdleFireLoop
from synapse_core.sessionend.tracker import SessionTracker

HOUR = 3600


def _mock_proc(poll_rc: int | None = None) -> MagicMock:
    m = MagicMock()
    m.poll.return_value = poll_rc
    return m


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make_jsonl(projects_dir: Path, project: str, sid: str, mtime: float) -> Path:
    p = projects_dir / project
    p.mkdir(parents=True, exist_ok=True)
    jsonl = p / f"{sid}.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (mtime, mtime))
    return jsonl


@pytest.fixture()
def env(tmp_path: Path):
    projects = tmp_path / "projects"
    markers = tmp_path / "markers"
    audit = tmp_path / "audit.log"
    err_log = tmp_path / "sessionend.err.log"
    state = tmp_path / "sessions.json"
    projects.mkdir()
    tracker = SessionTracker(state_path=state)
    return {
        "tmp": tmp_path,
        "projects": projects,
        "markers": markers,
        "audit": audit,
        "err_log": err_log,
        "tracker": tracker,
    }


def _build_loop(env, clock: FakeClock, mid_command: str = "") -> IdleFireLoop:
    return IdleFireLoop(
        sessions=env["tracker"],
        command_template="",
        marker_dir=env["markers"],
        audit_log=env["audit"],
        sessionend_err_log=env["err_log"],
        channel="wx",
        mid_sessionend_command=mid_command,
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
        spawn_probe_sec=0.0,
    )


def test_mid_fire_returns_false_when_command_empty(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00001"
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - HOUR)

    loop = _build_loop(env, clock)
    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None),
        patch("synapse_core.sessionend.idle.subprocess.Popen") as popen,
    ):
        fired = loop._maybe_mid_fire("u1", sid, clock.now)

    assert fired is False
    popen.assert_not_called()


def test_mid_fire_spawns_for_active_session(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00003"
    jsonl = _make_jsonl(env["projects"], "proj-x", sid, clock.now - HOUR)

    loop = IdleFireLoop(
        sessions=env["tracker"],
        command_template="",
        mid_sessionend_command=(
            "python -m marrow.mid_scan --sid {sid} --jsonl-path {jsonl} "
            "--channel {channel}"
        ),
        marker_dir=env["markers"],
        audit_log=env["audit"],
        sessionend_err_log=env["err_log"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
        spawn_probe_sec=0.0,
    )
    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None),
        patch("synapse_core.sessionend.idle.subprocess.Popen") as popen,
    ):
        fired = loop._maybe_mid_fire("u1", sid, clock.now)

    assert fired is True
    popen.assert_called_once()
    args, kwargs = popen.call_args
    assert args[0] == [
        "python",
        "-m",
        "marrow.mid_scan",
        "--sid",
        sid,
        "--jsonl-path",
        str(jsonl),
        "--channel",
        "wx",
    ]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["start_new_session"] is True
    assert (env["markers"] / f".mid_fired.{sid}").exists()
    assert f"kind=mid_scan sid={sid[:8]}" in env["audit"].read_text()


def test_mid_fire_marker_rate_limits(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00004"
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - HOUR)

    loop = IdleFireLoop(
        sessions=env["tracker"],
        command_template="",
        mid_sessionend_command="python -m marrow.mid_scan --sid {sid}",
        marker_dir=env["markers"],
        audit_log=env["audit"],
        sessionend_err_log=env["err_log"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
        spawn_probe_sec=0.0,
    )
    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None),
        patch("synapse_core.sessionend.idle.subprocess.Popen") as popen,
    ):
        first = loop._maybe_mid_fire("u1", sid, clock.now)
        clock.advance(60)
        second = loop._maybe_mid_fire("u1", sid, clock.now)

    assert first is True
    assert second is False
    assert popen.call_count == 1


def test_tick_once_calls_mid_fire(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00005"
    env["tracker"].set("u1", sid)
    loop = _build_loop(env, clock, mid_command="python -m marrow.mid_scan --sid {sid}")

    with (
        patch.object(loop, "_check_cross_channel") as cross,
        patch.object(loop, "_maybe_mid_fire", return_value=True) as maybe_mid_fire,
    ):
        fired = loop.tick_once()

    assert fired == []
    cross.assert_called_once_with("u1", sid)
    maybe_mid_fire.assert_called_once_with("u1", sid, clock.now)


def test_skips_sids_without_jsonl(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-no-jsonl")

    loop = _build_loop(env, clock, mid_command="python -m marrow.mid_scan --sid {sid}")
    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None),
        patch("synapse_core.sessionend.idle.subprocess.Popen") as popen,
    ):
        fired = loop.tick_once()

    assert fired == []
    popen.assert_not_called()


def test_forget_removes_from_scan(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-gggggggg")
    _make_jsonl(env["projects"], "proj-x", "sid-gggggggg", clock.now - HOUR)

    env["tracker"].forget("u1")
    loop = _build_loop(env, clock, mid_command="python -m marrow.mid_scan --sid {sid}")
    with patch("synapse_core.sessionend.idle.subprocess.Popen") as popen:
        fired = loop.tick_once()

    assert fired == []
    popen.assert_not_called()


def test_cross_channel_cleanup_forgets_session(env) -> None:
    clock = FakeClock()
    sid = "sid-cross0001"
    env["tracker"].set("u1", sid)

    claimed_sids: list[str] = []
    loop = IdleFireLoop(
        sessions=env["tracker"],
        command_template="",
        marker_dir=env["markers"],
        audit_log=env["audit"],
        sessionend_err_log=env["err_log"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
        spawn_probe_sec=0.0,
        claimed_away_hook=lambda s: claimed_sids.append(s),
    )

    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value="tg"),
        patch("synapse_core.replay_bookmark.save") as rb_save,
    ):
        loop._check_cross_channel("u1", sid)

    assert env["tracker"].get("u1") is None
    assert claimed_sids == [sid]
    rb_save.assert_called_once_with(sid, "wx")


def test_cross_channel_no_op_when_same_channel(env) -> None:
    clock = FakeClock()
    sid = "sid-cross0002"
    env["tracker"].set("u1", sid)

    loop = _build_loop(env, clock)
    with patch("synapse_core.sessionend.idle.session_lock.holder", return_value="wx"):
        loop._check_cross_channel("u1", sid)

    assert env["tracker"].get("u1") == sid


def test_cross_channel_no_op_when_no_owner(env) -> None:
    clock = FakeClock()
    sid = "sid-cross0003"
    env["tracker"].set("u1", sid)

    loop = _build_loop(env, clock)
    with patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None):
        loop._check_cross_channel("u1", sid)

    assert env["tracker"].get("u1") == sid
