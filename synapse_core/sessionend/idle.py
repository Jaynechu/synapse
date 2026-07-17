"""Idle-fire loop: scan tracked sessions for cross-channel handoff cleanup."""

from __future__ import annotations

import logging
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
    """Background loop: every scan_interval, reconcile tracked sessions.

    Cross-channel sessions (held by another channel) are cleaned up on each tick.
    """

    def __init__(
        self,
        *,
        sessions: SessionTracker,
        marker_dir: Path,
        audit_log: Path,
        channel: str,
        idle_threshold_sec: int = DEFAULT_IDLE_THRESHOLD_SEC,
        scan_interval_sec: int = DEFAULT_SCAN_INTERVAL_SEC,
        cc_projects_dir: Path = DEFAULT_CC_PROJECTS_DIR,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        claimed_away_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._sessions = sessions
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
        """Single scan pass. Reconciles cross-channel handoff for each sid."""
        snapshot = self._sessions.snapshot()
        for user_id, sid in snapshot.items():
            try:
                self._check_cross_channel(user_id, sid)
            except Exception as e:
                logger.warning("idle_fire scan failed for sid=%s: %s", sid[:8], e)
        return []

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

    # ── side effects ───────────────────────────────────────────────

    def _audit(self, line: str) -> None:
        try:
            self._audit_log.parent.mkdir(parents=True, exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            with self._audit_log.open("a") as f:
                f.write(f"[{today}] {line}\n")
        except OSError as e:
            logger.warning("audit write failed: %s", e)

