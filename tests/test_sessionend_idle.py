"""Tests for IdleFireLoop — threshold, marker-dedup, multi-fire, disabled-template, retry+alert."""

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
    """Return a Popen mock whose poll() returns poll_rc (None = still running)."""
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


def _build_loop(env, command_template: str, clock: FakeClock) -> IdleFireLoop:
    return IdleFireLoop(
        sessions=env["tracker"],
        command_template=command_template,
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


def test_fires_when_idle_over_threshold(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-aaaaaaaa")
    _make_jsonl(env["projects"], "proj-x", "sid-aaaaaaaa", clock.now - 7 * HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async --sid {sid}", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()) as popen:
        fired = loop.tick_once()

    assert fired == ["sid-aaaaaaaa"]
    popen.assert_called_once()
    args, kwargs = popen.call_args
    assert args[0] == ["python", "-m", "marrow.sessionend_async", "--sid", "sid-aaaaaaaa"]
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"].name == str(env["err_log"])
    assert kwargs["stdin"] is subprocess.DEVNULL

    marker = env["markers"] / ".fired.sid-aaaaaaaa"
    assert marker.exists()
    audit = env["audit"].read_text()
    assert "kind=idle_fire" in audit
    assert "sid=sid-aaaa" in audit
    assert "idle_hours=7.0" in audit


def test_does_not_fire_under_threshold(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-bbbbbbbb")
    _make_jsonl(env["projects"], "proj-x", "sid-bbbbbbbb", clock.now - 5 * HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen") as popen:
        fired = loop.tick_once()

    assert fired == []
    popen.assert_not_called()
    assert not (env["markers"] / ".fired.sid-bbbbbbbb").exists()


def test_mid_fire_returns_false_when_command_empty(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00001"
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)
    with (
        patch("synapse_core.sessionend.idle.session_lock.holder", return_value=None),
        patch("synapse_core.sessionend.idle.subprocess.Popen") as popen,
    ):
        fired = loop._maybe_mid_fire("u1", sid, clock.now)

    assert fired is False
    popen.assert_not_called()


def test_mid_fire_returns_false_when_session_is_idle(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00002"
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    loop = IdleFireLoop(
        sessions=env["tracker"],
        command_template="python -m marrow.sessionend_async {sid}",
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
        fired = loop._maybe_mid_fire("u1", sid, clock.now)

    assert fired is False
    popen.assert_not_called()


def test_mid_fire_spawns_for_active_session(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00003"
    jsonl = _make_jsonl(env["projects"], "proj-x", sid, clock.now - HOUR)

    loop = IdleFireLoop(
        sessions=env["tracker"],
        command_template="python -m marrow.sessionend_async {sid}",
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
        command_template="python -m marrow.sessionend_async {sid}",
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


def test_tick_once_calls_mid_fire_for_non_idle_fired_session(env) -> None:
    clock = FakeClock()
    sid = "sid-mid00005"
    env["tracker"].set("u1", sid)
    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)

    with (
        patch.object(loop, "_maybe_fire", return_value=False) as maybe_fire,
        patch.object(loop, "_maybe_mid_fire", return_value=True) as maybe_mid_fire,
    ):
        fired = loop.tick_once()

    assert fired == []
    maybe_fire.assert_called_once_with("u1", sid, clock.now)
    maybe_mid_fire.assert_called_once_with("u1", sid, clock.now)


def test_marker_blocks_refire_until_new_activity(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-cccccccc")
    _make_jsonl(env["projects"], "proj-x", "sid-cccccccc", clock.now - 7 * HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()):
        first = loop.tick_once()
        # next scan with no new activity — marker mtime >= jsonl mtime
        clock.advance(30 * 60)
        second = loop.tick_once()

    assert first == ["sid-cccccccc"]
    assert second == []


def test_new_activity_after_fire_allows_refire(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-dddddddd")
    jsonl = _make_jsonl(env["projects"], "proj-x", "sid-dddddddd", clock.now - 7 * HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()) as popen:
        first = loop.tick_once()
        # 7h pass; conversation resumes (touch jsonl); then idle 7h again
        clock.advance(14 * HOUR)
        new_mtime = clock.now - 7 * HOUR
        os.utime(jsonl, (new_mtime, new_mtime))
        second = loop.tick_once()

    assert first == ["sid-dddddddd"]
    assert second == ["sid-dddddddd"]
    assert popen.call_count == 2


def test_empty_command_template_no_spawn_but_marker_and_audit(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-eeeeeeee")
    _make_jsonl(env["projects"], "proj-x", "sid-eeeeeeee", clock.now - 8 * HOUR)

    loop = _build_loop(env, "", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen") as popen:
        fired = loop.tick_once()

    assert fired == ["sid-eeeeeeee"]
    popen.assert_not_called()
    assert (env["markers"] / ".fired.sid-eeeeeeee").exists()
    assert "kind=idle_fire" in env["audit"].read_text()


def test_command_template_substitutes_sid(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-ffffffff")
    _make_jsonl(env["projects"], "proj-x", "sid-ffffffff", clock.now - 7 * HOUR)

    loop = _build_loop(env, "/bin/echo prefix {sid} suffix", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()) as popen:
        loop.tick_once()

    args, _ = popen.call_args
    assert args[0] == ["/bin/echo", "prefix", "sid-ffffffff", "suffix"]


def test_skips_sids_without_jsonl(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-no-jsonl")
    # no jsonl created

    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen") as popen:
        fired = loop.tick_once()

    assert fired == []
    popen.assert_not_called()


def test_finds_jsonl_in_any_project_subdir(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-deepcafe")
    # nested under arbitrary project name
    _make_jsonl(env["projects"], "-Users-someone-weird-path", "sid-deepcafe", clock.now - 8 * HOUR)

    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen", return_value=_mock_proc()):
        fired = loop.tick_once()

    assert fired == ["sid-deepcafe"]


def test_forget_removes_from_scan(env) -> None:
    clock = FakeClock()
    env["tracker"].set("u1", "sid-gggggggg")
    _make_jsonl(env["projects"], "proj-x", "sid-gggggggg", clock.now - 7 * HOUR)

    env["tracker"].forget("u1")
    loop = _build_loop(env, "python -m marrow.sessionend_async {sid}", clock)
    with patch("synapse_core.sessionend.idle.subprocess.Popen") as popen:
        fired = loop.tick_once()

    assert fired == []
    popen.assert_not_called()


# ── retry + alert tests ────────────────────────────────────────────────────────


class FakeAlertSink:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def write(
        self,
        severity: str,
        kind: str,
        message: str,
        source: str = "",
        *,
        fingerprint: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "severity": severity,
                "kind": kind,
                "message": message,
                "source": source,
                "fingerprint": fingerprint,
            }
        )


def _build_loop_with_alerts(
    env, command_template: str, clock: FakeClock, alerts=None
) -> IdleFireLoop:
    return IdleFireLoop(
        sessions=env["tracker"],
        command_template=command_template,
        marker_dir=env["markers"],
        audit_log=env["audit"],
        sessionend_err_log=env["err_log"],
        channel="wx",
        idle_threshold_sec=6 * HOUR,
        scan_interval_sec=30 * 60,
        cc_projects_dir=env["projects"],
        clock=clock,
        sleeper=lambda _s: None,
        alerts=alerts,
        spawn_probe_sec=0.0,
    )


class _FakeProc:
    """Minimal Popen stand-in with a fixed poll() return value."""

    def __init__(self, poll_rc: int | None) -> None:
        self._rc = poll_rc

    def poll(self) -> int | None:
        return self._rc


def test_idle_fire_failure_first_attempt_writes_failed_marker(env) -> None:
    """First spawn returns rc=1 → .failed.{sid} written with count=1, no alert."""
    clock = FakeClock()
    sid = "sid-fail0001"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    alerts = FakeAlertSink()
    loop = _build_loop_with_alerts(env, "python -m marrow.sessionend_async {sid}", clock, alerts)

    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen",
        return_value=_FakeProc(poll_rc=1),
    ):
        fired = loop.tick_once()

    assert fired == []
    failed_marker = env["markers"] / f".failed.{sid}"
    assert failed_marker.exists()
    assert failed_marker.read_text().strip() == "1"
    assert not (env["markers"] / f".fired.{sid}").exists()
    assert alerts.calls == []
    audit = env["audit"].read_text()
    assert "kind=idle_fire_failed" in audit
    assert "attempts=1" in audit
    assert "alerted=0" in audit


def test_idle_fire_failure_second_attempt_alerts_and_clears(env) -> None:
    """Two consecutive rc=1 spawns → critical alert, .fired marker stamped, .failed cleared."""
    clock = FakeClock()
    sid = "sid-fail0002"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    alerts = FakeAlertSink()
    loop = _build_loop_with_alerts(env, "python -m marrow.sessionend_async {sid}", clock, alerts)

    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen",
        return_value=_FakeProc(poll_rc=1),
    ):
        loop.tick_once()  # first failure
        clock.advance(30 * 60)
        loop.tick_once()  # second failure → alert

    assert len(alerts.calls) == 1
    alert = alerts.calls[0]
    assert alert["severity"] == "critical"
    assert alert["kind"] == "sessionend_fire_failed"
    assert sid[:8] in alert["fingerprint"]
    assert "attempts=2" in alert["message"]
    assert alert["source"] == "synapse-wx/idle"

    assert (env["markers"] / f".fired.{sid}").exists()
    assert not (env["markers"] / f".failed.{sid}").exists()

    audit = env["audit"].read_text()
    assert "alerted=1" in audit


def test_idle_fire_success_after_failure_clears_failed_marker(env) -> None:
    """First tick rc=1 (fail), second tick poll()=None (running) → success, no alert."""
    clock = FakeClock()
    sid = "sid-fail0003"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    alerts = FakeAlertSink()
    loop = _build_loop_with_alerts(env, "python -m marrow.sessionend_async {sid}", clock, alerts)

    # first tick: spawn fails
    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen",
        return_value=_FakeProc(poll_rc=1),
    ):
        first = loop.tick_once()

    assert first == []
    assert (env["markers"] / f".failed.{sid}").exists()

    clock.advance(30 * 60)

    # second tick: spawn succeeds (process still running)
    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen",
        return_value=_FakeProc(poll_rc=None),
    ):
        second = loop.tick_once()

    assert second == [sid]
    assert (env["markers"] / f".fired.{sid}").exists()
    assert not (env["markers"] / f".failed.{sid}").exists()
    assert alerts.calls == []


def test_popen_oserror_treated_as_failure(env) -> None:
    """OSError from Popen → treated as first failure, .failed marker written."""
    clock = FakeClock()
    sid = "sid-oserr001"
    env["tracker"].set("u1", sid)
    _make_jsonl(env["projects"], "proj-x", sid, clock.now - 7 * HOUR)

    alerts = FakeAlertSink()
    loop = _build_loop_with_alerts(env, "/nonexistent/binary {sid}", clock, alerts)

    with patch(
        "synapse_core.sessionend.idle.subprocess.Popen",
        side_effect=OSError("no such file"),
    ):
        fired = loop.tick_once()

    assert fired == []
    failed_marker = env["markers"] / f".failed.{sid}"
    assert failed_marker.exists()
    assert failed_marker.read_text().strip() == "1"
    assert alerts.calls == []
