"""Direct sqlite access to marrow ``outbox`` for wx outbound delivery.

Mirrors synapse_tg/outbox.py (target='wx'). Touches outbox rows/status columns
only (precedent: commands/marrow_audit.py). Delivery is at-most-once per row: a
single UPDATE...WHERE status='pending' claims a row (rowcount decides the winner
across processes). No whole-row redelivery after a crash — a stale 'claimed' row
from a previous run is failed, never resent (duplicate delivery to her phone is
worse than a lost note).
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 5000


def _connect(db_path: str | Path | None) -> sqlite3.Connection | None:
    if not db_path:
        return None
    p = Path(db_path)
    if not p.is_file():
        logger.warning("outbox: db missing at %s — skipping", p)
        return None
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.warning("outbox: open failed (%s)", e)
        return None


def claim_pending(db_path: str | Path | None, target: str = "wx") -> list[sqlite3.Row]:
    """Atomically claim all pending rows for ``target``. Returns claimed rows.

    Claim is per-row: UPDATE...WHERE status='pending' then re-select the rows now
    marked 'claimed'. The single UPDATE is the race winner across processes.
    """
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        with conn:
            cur = conn.execute(
                "UPDATE outbox SET status='claimed'"
                " WHERE status='pending' AND target=?",
                (target,),
            )
            if cur.rowcount == 0:
                return []
            rows = conn.execute(
                "SELECT id, body, retry_count FROM outbox"
                " WHERE status='claimed' AND target=? ORDER BY id",
                (target,),
            ).fetchall()
        return list(rows)
    except sqlite3.Error as e:
        logger.warning("outbox claim failed: %s", e)
        return []
    finally:
        conn.close()


def mark_sent(db_path: str | Path | None, row_id: int) -> None:
    _update_status(
        db_path,
        "UPDATE outbox SET status='sent',"
        " sent_at=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE id=?",
        (row_id,),
        "mark_sent",
    )


def mark_failed(db_path: str | Path | None, row_id: int, retry_count: int | None = None) -> None:
    if retry_count is None:
        _update_status(
            db_path,
            "UPDATE outbox SET status='failed' WHERE id=?",
            (row_id,),
            "mark_failed",
        )
    else:
        _update_status(
            db_path,
            "UPDATE outbox SET status='failed', retry_count=? WHERE id=?",
            (retry_count, row_id),
            "mark_failed",
        )


def sweep_orphan_claimed(db_path: str | Path | None, target: str = "wx") -> list[int]:
    """Fail any stale 'claimed' row for ``target`` (crash orphan). Returns ids.

    Call at startup only: mid-run, 'claimed' rows are in flight within one poll.
    """
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        with conn:
            rows = conn.execute(
                "SELECT id FROM outbox WHERE status='claimed' AND target=?",
                (target,),
            ).fetchall()
            if not rows:
                return []
            conn.execute(
                "UPDATE outbox SET status='failed'"
                " WHERE status='claimed' AND target=?",
                (target,),
            )
        return [r["id"] for r in rows]
    except sqlite3.Error as e:
        logger.warning("outbox orphan sweep failed: %s", e)
        return []
    finally:
        conn.close()


def _update_status(
    db_path: str | Path | None, sql: str, params: tuple, label: str
) -> None:
    conn = _connect(db_path)
    if conn is None:
        return
    try:
        with conn:
            conn.execute(sql, params)
    except sqlite3.Error as e:
        logger.warning("outbox %s failed: %s", label, e)
    finally:
        conn.close()
