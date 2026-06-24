"""Idle-fire loop: scan tracked sessions, fire sessionend command after 6h inactivity."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from synapse_core import session_lock

from .tracker import SessionTracker

logger = logging.getLogger(__name__)

DEFAULT_IDLE_THRESHOLD_SEC = 6 * 3600
DEFAULT_SCAN_INTERVAL_SEC = 30 * 60
DEFAULT_CC_PROJECTS_DIR = Path.home() / ".claude" / "projects"


class IdleFireLoop:
    """Background loop: every scan_interval, fire sessionend for sids idle >= threshold.

    cc subprocess is untouched — sessionend runs out-of-band as a detached subprocess.
    Multiple fires per sid per day are allowed: marker resets when jsonl mtime advances.
    Empty command_template disables actual subprocess spawn (audit + marker still recorded).

    Retry behaviour: if spawn fails (non-zero rc or OSError), a .failed.{sid} marker
    records the attempt count. On the next scan tick, one retry is attempted. If that also
    fails, AlertSink.write() is called (severity=critical), the fired marker is stamped to
    stop further retries for this jsonl_mtime epoch, and the failed marker is cleared.
    """

    def __init__(
        self,
        *,
        sessions: SessionTracker,
        command_template: str,
        marker_dir: Path,
        audit_log: Path,
        sessionend_err_log: Path,
        channel: str,
        mid_sessionend_command: str = "",
        idle_threshold_sec: int = DEFAULT_IDLE_THRESHOLD_SEC,
        scan_interval_sec: int = DEFAULT_SCAN_INTERVAL_SEC,
        cc_projects_dir: Path = DEFAULT_CC_PROJECTS_DIR,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        alerts=None,
        spawn_probe_sec: float = 3.0,
        pre_spawn_hook: Callable[[str], None] | None = None,
        claimed_away_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._command_template = command_template or ""
        self._mid_command = mid_sessionend_command or ""
        self._idle_threshold_sec = idle_threshold_sec
        self._scan_interval_sec = scan_interval_sec
        self._cc_projects_dir = Path(cc_projects_dir)
        self._marker_dir = Path(marker_dir)
        self._audit_log = Path(audit_log)
        self._sessionend_err_log = Path(sessionend_err_log)
        self._channel = channel
        self._clock = clock
        self._sleeper = sleeper
        self._alerts = alerts
        self._spawn_probe_sec = spawn_probe_sec
        # B11: invoked with sid BEFORE sessionend_async spawn. Wired to
        # MainLoop.idle_close_provider so live cc closes first — cc-side
        # SessionEnd hook archives events + writes bridge_owns marker. sid is
        # NOT cleared from SessionTracker (next inbound lazy-resumes).
        self._pre_spawn_hook = pre_spawn_hook
        self._claimed_away_hook = claimed_away_hook

        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"synapse-{self._channel}-idle-fire", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._sleeper(self._scan_interval_sec)
                if self._stop_evt.is_set():
                    break
                self.tick_once()
            except Exception as e:
                logger.warning("idle_fire_loop error: %s", e)

    # ── core scan ──────────────────────────────────────────────────

    def tick_once(self) -> list[str]:
        """Single scan pass. Returns list of sids fired this tick."""
        fired: list[str] = []
        now = self._clock()
        snapshot = self._sessions.snapshot()
        for user_id, sid in snapshot.items():
            try:
                if self._maybe_fire(user_id, sid, now):
                    fired.append(sid)
                elif self._maybe_mid_fire(user_id, sid, now):
                    pass
            except Exception as e:
                logger.warning("idle_fire scan failed for sid=%s: %s", sid[:8], e)
        return fired

    def _maybe_fire(self, user_id: str, sid: str, now: float) -> bool:
        if not sid:
            return False
        # Skip if another channel holds this session (cross-channel resume).
        owner = session_lock.holder(sid)
        if owner and owner != self._channel:
            logger.info("idle: sid=%s claimed by %s, cleaning up", sid[:8], owner)
            if self._claimed_away_hook:
                try:
                    self._claimed_away_hook(sid)
                except Exception as e:
                    logger.warning("claimed_away_hook failed: %s", e)
            try:
                from synapse_core import replay_bookmark
                replay_bookmark.save(sid, self._channel)
            except Exception:
                pass
            self._sessions.forget(user_id)
            return False
        jsonl = self._find_jsonl(sid)
        if jsonl is None:
            return False
        jsonl_mtime = jsonl.stat().st_mtime
        idle = now - jsonl_mtime
        if idle < self._idle_threshold_sec:
            return False

        fired_marker = self._marker_dir / f".fired.{sid}"
        if fired_marker.exists() and fired_marker.stat().st_mtime >= jsonl_mtime:
            return False

        idle_hours = idle / 3600
        attempts = self._read_failed_attempts(sid)
        # B11: close live cc FIRST so its SessionEnd hook archives events +
        # writes bridge_owns marker. Failure here must NOT block sessionend
        # spawn — worst case we lose live-archive and fall back to the
        # marrow catchup TTL.
        if self._pre_spawn_hook is not None:
            try:
                self._pre_spawn_hook(sid)
            except Exception as e:
                logger.warning(
                    "pre_spawn_hook failed for sid=%s: %s", sid[:8], e
                )
        success = self._spawn(sid)

        if success:
            self._touch_marker(fired_marker)
            self._clear_failed(sid)
            attempt_num = attempts + 1
            self._audit(
                f"kind=idle_fire sid={sid[:8]} idle_hours={idle_hours:.1f} attempt={attempt_num}"
            )
            logger.info("Idle fire: sid=%s idle=%.1fh", sid[:8], idle_hours)
            return True
        else:
            new_attempts = attempts + 1
            if new_attempts >= 2:
                if self._alerts is not None:
                    self._alerts.write(
                        "critical",
                        "sessionend_fire_failed",
                        f"attempts={new_attempts}",
                        source=f"synapse-{self._channel}/idle",
                        fingerprint=f"sessionend_fire_failed:sid={sid[:8]}",
                    )
                self._touch_marker(fired_marker)
                self._clear_failed(sid)
                self._audit(
                    f"kind=idle_fire_failed sid={sid[:8]} attempts={new_attempts} alerted=1"
                )
                logger.warning(
                    "Idle fire failed after %d attempts, alerting: sid=%s",
                    new_attempts,
                    sid[:8],
                )
            else:
                self._write_failed_attempts(sid, new_attempts)
                self._audit(
                    f"kind=idle_fire_failed sid={sid[:8]} attempts={new_attempts} alerted=0"
                )
                logger.warning(
                    "Idle fire attempt %d failed, will retry next tick: sid=%s",
                    new_attempts,
                    sid[:8],
                )
            return False

    def _maybe_mid_fire(self, user_id: str, sid: str, now: float) -> bool:
        if not self._mid_command or not sid:
            return False
        owner = session_lock.holder(sid)
        if owner and owner != self._channel:
            return False
        jsonl = self._find_jsonl(sid)
        if jsonl is None:
            return False
        idle = now - jsonl.stat().st_mtime
        if idle >= self._idle_threshold_sec:
            return False

        mid_marker = self._marker_dir / f".mid_fired.{sid}"
        if (
            mid_marker.exists()
            and now - mid_marker.stat().st_mtime < self._scan_interval_sec
        ):
            return False

        cmd_str = (
            self._mid_command
            .replace("{sid}", sid)
            .replace("{jsonl}", str(jsonl))
            .replace("{channel}", self._channel)
        )
        argv = shlex.split(cmd_str)
        if not argv:
            return False
        try:
            subprocess.Popen(  # noqa: S603 - cmd template is operator-supplied config
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError) as e:
            logger.warning(
                "Failed to spawn mid-session scan for sid=%s: %s", sid[:8], e
            )
            return False

        self._touch_marker(mid_marker)
        self._audit(f"kind=mid_scan sid={sid}")
        return True

    def _find_jsonl(self, sid: str) -> Path | None:
        if not self._cc_projects_dir.is_dir():
            return None
        # sid may live under any project subdir
        for project_dir in self._cc_projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{sid}.jsonl"
            if candidate.is_file():
                return candidate
        return None

    # ── side effects ───────────────────────────────────────────────

    def _spawn(self, sid: str) -> bool:
        """Attempt to spawn the sessionend subprocess. Returns True on success."""
        if not self._command_template:
            # audit-only mode: no real subprocess, treat as success
            return True
        cmd_str = self._command_template.replace("{sid}", sid)
        argv = shlex.split(cmd_str)
        if not argv:
            return True
        try:
            proc = subprocess.Popen(  # noqa: S603 - cmd template is operator-supplied config
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=open(self._sessionend_err_log, "ab"),  # noqa: SIM115
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, FileNotFoundError) as e:
            logger.warning("Failed to spawn sessionend for sid=%s: %s", sid[:8], e)
            return False

        self._sleeper(self._spawn_probe_sec)
        rc = proc.poll()
        if rc is None:
            # still running — marrow takes over from here
            return True
        if rc == 0:
            # exited cleanly — treat as success (may be a no-op for this sid)
            return True
        # non-zero exit within probe window
        logger.warning(
            "sessionend subprocess exited rc=%d within probe window for sid=%s",
            rc,
            sid[:8],
        )
        return False

    def _touch_marker(self, marker: Path) -> None:
        self._marker_dir.mkdir(parents=True, exist_ok=True)
        marker.touch()
        # ensure mtime == now even if marker pre-existed
        ts = self._clock()
        try:
            os.utime(marker, (ts, ts))
        except OSError:
            pass

    def _audit(self, line: str) -> None:
        try:
            self._audit_log.parent.mkdir(parents=True, exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            with self._audit_log.open("a") as f:
                f.write(f"[{today}] {line}\n")
        except OSError as e:
            logger.warning("audit write failed: %s", e)

    # ── failed-attempt marker helpers ──────────────────────────────

    def _failed_marker_path(self, sid: str) -> Path:
        return self._marker_dir / f".failed.{sid}"

    def _read_failed_attempts(self, sid: str) -> int:
        """Return recorded failure count (0 if no file or unreadable)."""
        path = self._failed_marker_path(sid)
        try:
            return int(path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _write_failed_attempts(self, sid: str, n: int) -> None:
        """Atomically write attempt count to .failed.{sid}."""
        self._marker_dir.mkdir(parents=True, exist_ok=True)
        path = self._failed_marker_path(sid)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(f"{n}\n")
        os.replace(tmp, path)

    def _clear_failed(self, sid: str) -> None:
        """Remove .failed.{sid} if it exists."""
        try:
            self._failed_marker_path(sid).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Failed to clear failed marker for sid=%s: %s", sid[:8], e)
