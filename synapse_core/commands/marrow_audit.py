"""Direct sqlite writer for marrow ``audit_log`` rows used by ``mm-`` / ``mm+``.

Background
----------
marrow's hooks layer already owns these flags (see
``marrow/marrow/hooks.py::_write_manual_skip_flag`` and ``_write_session_block_flag``):

    INSERT INTO audit_log (target_table, target_id, action, summary)
    VALUES ('events', <sid>, 'manual_skip',   'skip' | 'skip_cleared')
    INSERT INTO audit_log (target_table, target_id, action, summary)
    VALUES ('events', <sid>, 'session_block', 'archive' | 'cleared')

Marrow's CLI (``mw``) does NOT expose an ``audit-log`` subcommand at the time of
this commit — only ``add-session`` / ``get-session-model`` /
``list-recent-sessions`` / ``add-alert``. Rather than block on a marrow-side CLI
addition, the bridge writes the two rows directly to the marrow sqlite file. The
schema is stable and shared (audit_log has lived in marrow since v1) and we use
the SAME column order + SAME values that marrow's internal helpers use, so
``_is_manual_skip`` / ``_is_session_blocked`` on the marrow side will read these
rows back identically.

Best-effort: every error path is a silent no-op + log so the WeChat loop is
never blocked by a marrow-db issue.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# Mirror of marrow.hooks constants. Kept inline so the bridge does NOT import
# from the marrow package (synapse-wx is meant to run even when marrow is
# absent — only the sqlite file matters).
_TARGET_TABLE = "events"
_ACTION_MANUAL_SKIP = "manual_skip"
_ACTION_SESSION_BLOCK = "session_block"


def _open(db_path: str | Path | None) -> sqlite3.Connection | None:
    if not db_path:
        return None
    p = Path(db_path)
    if not p.is_file():
        logger.warning("marrow_audit: db missing at %s — skipping write", p)
        return None
    try:
        return sqlite3.connect(str(p), timeout=5.0)
    except sqlite3.Error as e:
        logger.warning("marrow_audit: open failed (%s)", e)
        return None


def _insert(
    db_path: str | Path | None,
    sid: str | None,
    action: str,
    summary: str,
) -> None:
    if not sid:
        return
    conn = _open(db_path)
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO audit_log (target_table, target_id, action, summary)"
                " VALUES (?, ?, ?, ?)",
                (_TARGET_TABLE, sid, action, summary),
            )
    except sqlite3.Error as e:
        logger.warning("marrow_audit insert (%s=%s) failed: %s", action, summary, e)
    finally:
        conn.close()


def write_skip(db_path: str | Path | None, sid: str | None, status: str) -> None:
    """Append a ``manual_skip`` row. ``status`` is ``"skip"`` or ``"skip_cleared"``."""
    _insert(db_path, sid, _ACTION_MANUAL_SKIP, status)


def write_block(db_path: str | Path | None, sid: str | None, status: str) -> None:
    """Append a ``session_block`` row. ``status`` is ``"archive"`` or ``"cleared"``."""
    _insert(db_path, sid, _ACTION_SESSION_BLOCK, status)


def write_extract(db_path: str | Path | None, sid: str | None, status: str) -> None:
    """Append a ``sessionend_extract`` row (e.g. ``reset:mm_plus``)."""
    _insert(db_path, sid, "sessionend_extract", status)
