"""Watch + kick: wake cortex from a bridge inbound / outbox poll (P6).

Shared by both bridges (tg/wx) — the watch-claim SQL and the kick spawn are
channel-agnostic (parameterized by target). The bridge supplies its own "from
her" identity check before calling any claim here.

Kick = a detached, fire-and-forget subprocess in the cortex venv (bridges cannot
import cortex). The command lives in synapse [outbox].kick_cmd; absent = the
feature no-ops with a one-time warning. Never blocks the bridge.

Three claim paths, all resolved by a single atomic UPDATE (armed->fired), so a
reply and a timeout racing the same row produce exactly one winner -> one kick:
  - reply:   inbound from her on channel X -> claim ALL armed watches on X.
  - timeout: poll finds sent+armed rows past their timeout, confirmed no reply
             via the marrow events table, then claims each.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import sqlite3
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 5000
_warned_no_cmd = False


def _connect(db_path: str | Path | None) -> sqlite3.Connection | None:
    if not db_path:
        return None
    p = Path(db_path)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect(str(p), timeout=5.0)
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.warning("cortex_kick: db open failed (%s)", e)
        return None


def _kick_argv(kick_cmd) -> list[str] | None:
    if not kick_cmd:
        return None
    if isinstance(kick_cmd, (list, tuple)):
        argv = [str(x) for x in kick_cmd if str(x).strip()]
    else:
        argv = shlex.split(str(kick_cmd))
    return argv or None


def kick(kick_cmd, kind: str, *, note_id=None, minutes=None, text=None,
         text_chars=None) -> bool:
    """Spawn one detached cortex.kick. Returns True if launched. Absent kick_cmd
    warns once, then no-ops. Never raises. `text` (her reply) is truncated to
    `text_chars` and passed as --text so the wakeup note shows WHAT she said."""
    global _warned_no_cmd
    argv = _kick_argv(kick_cmd)
    if argv is None:
        if not _warned_no_cmd:
            logger.warning(
                "cortex_kick: [outbox].kick_cmd not set — watch/kick disabled")
            _warned_no_cmd = True
        return False
    argv = argv + ["--kind", str(kind)]
    if note_id is not None:
        argv += ["--note-id", str(note_id)]
    if minutes is not None:
        argv += ["--minutes", str(minutes)]
    if text:
        t = str(text)
        if text_chars and text_chars > 0:
            t = t[:int(text_chars)]
        argv += ["--text", t]
    try:
        subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ},
        )
        return True
    except OSError as e:
        logger.warning("cortex_kick: spawn failed (%s)", e)
        return False


def claim_reply(db_path, channel: str) -> list[int]:
    """Inbound from her on `channel`: atomically claim ALL armed watches whose
    note went to that channel (single UPDATE armed->fired). Returns the note ids
    claimed (empty when none / no db). The single UPDATE is the race winner
    against a concurrent timeout claim."""
    conn = _connect(db_path)
    if conn is None:
        return []
    try:
        with conn:
            rows = conn.execute(
                "SELECT id FROM outbox WHERE target=? AND status='sent'"
                " AND watch_reply=1 AND watch_state='armed'",
                (channel,),
            ).fetchall()
            if not rows:
                return []
            conn.execute(
                "UPDATE outbox SET watch_state='fired' WHERE target=?"
                " AND status='sent' AND watch_reply=1 AND watch_state='armed'",
                (channel,),
            )
        return [r["id"] for r in rows]
    except sqlite3.Error as e:
        logger.warning("cortex_kick claim_reply failed: %s", e)
        return []
    finally:
        conn.close()


def stamp_receipts(db_path, channel: str, text: str, text_chars=None,
                    inbound_at: str | None = None) -> int:
    """Inbound from her on `channel`: stamp a reply receipt (replied_at UTC ISO +
    truncated reply_text) on every sent note delivered to that channel still
    awaiting one (replied_at IS NULL), watch or not. Single UPDATE. Returns the
    row count stamped (0 on none / no db). Never raises — the durable record is
    best-effort, same defensive style as claim_reply.

    `inbound_at` (UTC ISO string, same format as outbox.sent_at) bounds the
    stamp to rows sent at/before her inbound message: a same-poll batch that
    delivers an outbound note AND processes her earlier inbound in one pass
    must not stamp the just-sent note as if she'd already replied to it.
    None (caller has no timestamp) falls back to the unbounded scope."""
    conn = _connect(db_path)
    if conn is None:
        return 0
    body = str(text or "")
    if text_chars and text_chars > 0:
        body = body[:int(text_chars)]
    where = "target=? AND status='sent' AND replied_at IS NULL"
    params: list = [body, channel]
    if inbound_at:
        where += " AND sent_at <= ?"
        params.append(inbound_at)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE outbox SET replied_at=strftime('%Y-%m-%dT%H:%M:%SZ','now'),"
                f" reply_text=? WHERE {where}",
                params,
            )
        return cur.rowcount or 0
    except sqlite3.Error as e:
        logger.warning("cortex_kick stamp_receipts failed: %s", e)
        return 0
    finally:
        conn.close()


def _no_user_reply_since(conn: sqlite3.Connection, channel: str, sent_at: str) -> bool:
    """True when the marrow events table shows NO real user turn on `channel`
    after `sent_at` (per-turn stop-hook archiving = the cross-process truth,
    survives bridge restart). A row present -> she replied -> not a timeout."""
    try:
        row = conn.execute(
            "SELECT 1 FROM events WHERE role='user' AND channel=?"
            " AND timestamp > ? LIMIT 1",
            (channel, sent_at),
        ).fetchone()
    except sqlite3.Error:
        return False  # cannot confirm silence -> do NOT fire a false timeout
    return row is None


def claim_timeouts(db_path, channel: str, timeout_min_default=None) -> list[dict]:
    """Poll path: find sent+armed rows on `channel` whose timeout elapsed. When
    the events table confirms no reply, atomically claim (armed->fired) + kick.
    When she DID reply before the deadline, atomically claim (armed->'satisfied')
    and do NOT kick — the watch is done, so it is not re-polled every tick.
    Returns [{id, minutes}] for the fired rows this call won. The per-row
    single-row UPDATE resolves any race with a concurrent reply claim (one
    winner)."""
    conn = _connect(db_path)
    if conn is None:
        return []
    won: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT id, sent_at, watch_timeout_min FROM outbox"
            " WHERE target=? AND status='sent' AND watch_state='armed'"
            " AND watch_timeout_min IS NOT NULL AND sent_at IS NOT NULL"
            " AND strftime('%Y-%m-%dT%H:%M:%SZ','now') >"
            " strftime('%Y-%m-%dT%H:%M:%SZ', sent_at, '+' || watch_timeout_min || ' minutes')",
            (channel,),
        ).fetchall()
        for r in rows:
            if not _no_user_reply_since(conn, channel, r["sent_at"]):
                # She replied before the deadline: retire the watch silently so
                # this row stops being re-checked on every poll.
                with conn:
                    conn.execute(
                        "UPDATE outbox SET watch_state='satisfied'"
                        " WHERE id=? AND watch_state='armed'",
                        (r["id"],),
                    )
                continue
            with conn:
                cur = conn.execute(
                    "UPDATE outbox SET watch_state='fired'"
                    " WHERE id=? AND watch_state='armed'",
                    (r["id"],),
                )
            if cur.rowcount == 1:
                won.append({"id": r["id"], "minutes": r["watch_timeout_min"]})
        return won
    except sqlite3.Error as e:
        logger.warning("cortex_kick claim_timeouts failed: %s", e)
        return won
    finally:
        conn.close()


def night_mode(wake_state_file) -> bool:
    """True when cortex wake_state.json carries mode=='night'. Absent / missing
    file -> False. The flag lifecycle is P8; this only reads it."""
    if not wake_state_file:
        return False
    p = Path(wake_state_file).expanduser()
    try:
        if not p.is_file():
            return False
        d = json.loads(p.read_text())
        return str(d.get("mode") or "") == "night"
    except (OSError, ValueError):
        return False


def past_morning_start(morning_start: str, tz_name: str) -> bool:
    """True when local time is at/after `morning_start` ("HH:MM")."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        hh, mm = (int(x) for x in str(morning_start or "06:00").split(":"))
    except (ValueError, TypeError):
        hh, mm = 6, 0
    try:
        now = datetime.now(ZoneInfo(tz_name or "Australia/Melbourne"))
    except Exception:
        now = datetime.now()
    return (now.hour, now.minute) >= (hh, mm)
