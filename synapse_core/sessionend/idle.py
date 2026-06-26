"""Idle-fire loop: scan tracked sessions, trigger mid-session scan command."""

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
    """Background loop: every scan_interval, trigger mid-session scan for active sids.

    Cross-channel sessions (held by another channel) are cleaned up on each tick.
    mid_sessionend_command is spawned as a detached subprocess; rate-limited per
    scan_interval via a .mid_fired.{sid} marker.
    """

    def __init__(
        self,
        *,
        sessions: SessionTracker,
        marker_dir: Path,
        audit_log: Path,
        channel: str,
        mid_sessionend_command: str = "",
        idle_threshold_sec: int = DEFAULT_IDLE_THRESHOLD_SEC,
        scan_interval_sec: int = DEFAULT_SCAN_INTERVAL_SEC,
        cc_projects_dir: Path = DEFAULT_CC_PROJECTS_DIR,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        claimed_away_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._mid_command = mid_sessionend_command or ""
        self._idle_threshold_sec = idle_threshold_sec
        self._scan_interval_sec = scan_interval_sec
        self._cc_projects_dir = Path(cc_projects_dir)
        self._marker_dir = Path(marker_dir)
        self._audit_log = Path(audit_log)
        self._channel = channel
        self._clock = clock
        self._sleeper = sleeper
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
                self._check_cross_channel(user_id, sid)
                if self._maybe_mid_fire(user_id, sid, now):
                    pass
            except Exception as e:
                logger.warning("idle_fire scan failed for sid=%s: %s", sid[:8], e)
        return fired

    def _check_cross_channel(self, user_id: str, sid: str) -> None:
        if not sid:
            return
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

    def _maybe_mid_fire(self, user_id: str, sid: str, now: float) -> bool:
        if not self._mid_command or not sid:
            return False
        owner = session_lock.holder(sid)
        if owner and owner != self._channel:
            return False
        jsonl = self._find_jsonl(sid)
        if jsonl is None:
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
        self._audit(f"kind=mid_scan sid={sid[:8]}")
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

