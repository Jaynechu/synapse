"""pre_spawn_hook is no longer a parameter of IdleFireLoop — verify it is not invoked."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from synapse_core.sessionend.idle import IdleFireLoop
from synapse_core.sessionend.tracker import SessionTracker

HOUR = 3600


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


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


def _build_loop(env, clock: FakeClock) -> IdleFireLoop:
    return IdleFireLoop(
        sessions=env["tracker"],
        mid_sessionend_command="python -m marrow.mid_scan --sid {sid}",
        marker_dir=env["markers"],
        audit_log=env["audit"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
    )


def test_pre_spawn_hook_removed_loop_ticks_without_error(env) -> None:
    """Loop constructs and ticks without raising after pre_spawn_hook removal."""
    clock = FakeClock()
    sid = "sid-b11a0001"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - HOUR)

    loop = _build_loop(env, clock)

    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None),
        patch("synapse_core.sessionend.idle.subprocess.Popen"),
    ):
        loop.tick_once()


def test_loop_mid_fires_without_pre_spawn_hook(env) -> None:
    """Mid-session scan fires correctly with no pre_spawn_hook parameter."""
    clock = FakeClock()
    sid = "sid-b11a0002"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - HOUR)

    loop = _build_loop(env, clock)

    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None),
        patch("synapse_core.sessionend.idle.subprocess.Popen") as popen,
    ):
        loop.tick_once()

    popen.assert_called_once()
