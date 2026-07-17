"""Tests for IdleFireLoop — cross-channel handoff cleanup."""

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


def _build_loop(env, clock: FakeClock) -> IdleFireLoop:
    return IdleFireLoop(
        sessions=env["tracker"],
        marker_dir=env["markers"],
        audit_log=env["audit"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
    )


def test_tick_once_checks_cross_channel(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00005"
    env["tracker"].set("u1", sid)
    loop = _build_loop(env, clock)

    with patch.object(loop, "_check_cross_channel") as cross:
        fired = loop.tick_once()

    assert fired == []
    cross.assert_called_once_with("u1", sid)


def test_forget_removes_from_scan(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-gggggggg")
    _make_jsonl(env["projects"], "proj-x", "sid-gggggggg", clock.now - HOUR)

    env["tracker"].forget("u1")
    loop = _build_loop(env, clock)
    with patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None):
        fired = loop.tick_once()

    assert fired == []


def test_cross_channel_cleanup_forgets_session(env) -> None:
    clock = FakeClock()
    sid = "sid-cross0001"
    env["tracker"].set("u1", sid)

    claimed_sids: list[str] = []
    loop = IdleFireLoop(
        sessions=env["tracker"],
        marker_dir=env["markers"],
        audit_log=env["audit"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
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
