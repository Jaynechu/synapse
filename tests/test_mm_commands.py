"""Tests for B8 mm- / mm+ literal commands.

These are single-line bare-text commands (no leading `/`). On match the bridge
writes two marrow `audit_log` rows for the current sid:

  mm-  →  manual_skip = "skip"          AND  session_block = "archive"
  mm+  →  manual_skip = "skip_cleared"  AND  force_sessionend = "mm_plus_flag"

mm+ deliberately does not clear session_block; mm- keeps its no-ingestion
contract.
"""

from __future__ import annotations

import sqlite3

from synapse_core.commands import marrow_audit
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState

# ── marrow_audit module (direct sqlite writer) ───────────────────────────────


def _make_audit_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE audit_log (
          id INTEGER PRIMARY KEY,
          target_table TEXT NOT NULL,
          target_id TEXT,
          action TEXT NOT NULL,
          summary TEXT,
          occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
        """
    )
    conn.commit()
    conn.close()


def test_write_skip_inserts_manual_skip_row(tmp_path) -> None:
    db = tmp_path / "marrow.db"
    _make_audit_db(db)
    marrow_audit.write_skip(str(db), "sid-1", "skip")

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT target_table, target_id, action, summary FROM audit_log"
    ).fetchone()
    conn.close()
    assert row == ("events", "sid-1", "manual_skip", "skip")


def test_write_block_inserts_session_block_row(tmp_path) -> None:
    db = tmp_path / "marrow.db"
    _make_audit_db(db)
    marrow_audit.write_block(str(db), "sid-2", "archive")

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT target_table, target_id, action, summary FROM audit_log"
    ).fetchone()
    conn.close()
    assert row == ("events", "sid-2", "session_block", "archive")


def test_write_force_inserts_force_sessionend_row(tmp_path) -> None:
    db = tmp_path / "marrow.db"
    _make_audit_db(db)
    marrow_audit.write_force(str(db), "sid-3", "mm_plus_flag")

    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT target_table, target_id, action, summary FROM audit_log"
    ).fetchone()
    conn.close()
    assert row == ("events", "sid-3", "force_sessionend", "mm_plus_flag")


def test_writer_no_db_path_is_noop(tmp_path) -> None:
    # Empty / None path = marrow not configured; must not raise.
    marrow_audit.write_skip("", "sid-x", "skip")
    marrow_audit.write_block(None, "sid-x", "archive")  # type: ignore[arg-type]


def test_writer_missing_db_is_noop(tmp_path) -> None:
    # Path points at a non-existent file; writer must swallow.
    bogus = tmp_path / "does_not_exist" / "marrow.db"
    marrow_audit.write_skip(str(bogus), "sid-x", "skip")


def test_writer_no_sid_is_noop(tmp_path) -> None:
    db = tmp_path / "marrow.db"
    _make_audit_db(db)
    marrow_audit.write_skip(str(db), "", "skip")
    marrow_audit.write_block(str(db), None, "archive")  # type: ignore[arg-type]
    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    conn.close()
    assert count == 0


# ── registry dispatch of `mm-` / `mm+` literals ──────────────────────────────


def _make_registry(sid: str | None = "sid-current"):
    state = BridgeState(model="claude-opus-4-7[1m]", session_id=sid)
    calls: list[tuple[str, str, str]] = []  # (kind, sid, status)

    def writer(kind: str, sid_arg: str, status: str) -> None:
        calls.append((kind, sid_arg, status))

    ctx = CommandContext(
        state=state,
        swap_provider=lambda *_a, **_kw: None,
        close_provider=lambda: None,
        forget_session=lambda: None,
        audit_writer=writer,
    )
    return Registry(ctx), calls, state


def test_mm_minus_writes_skip_and_block() -> None:
    reg, calls, _ = _make_registry()
    verdict, reply = reg.dispatch("mm-")
    assert verdict == "handled"
    assert reply is not None
    assert reply == "本窗口跳过DB"
    assert calls == [
        ("manual_skip", "sid-current", "skip"),
        ("session_block", "sid-current", "archive"),
    ]


def test_mm_plus_clears_skip_and_flags_sessionend() -> None:
    reg, calls, _ = _make_registry()
    verdict, reply = reg.dispatch("mm+")
    assert verdict == "handled"
    assert reply == "本窗口加入DB"
    assert calls == [
        ("manual_skip", "sid-current", "skip_cleared"),
        ("session_block", "sid-current", "cleared"),
        ("force_sessionend", "sid-current", "mm_plus_flag"),
    ]


def test_mm_literals_case_sensitive_lowercase_only() -> None:
    # Spec is exact `mm-` / `mm+` — uppercase falls through to forward so we
    # do not steal a sentence that happens to start with "MM-".
    reg, calls, _ = _make_registry()
    assert reg.dispatch("MM-") == ("forward", None)
    assert calls == []


def test_mm_with_payload_falls_through_to_forward() -> None:
    # Literal command means whole-message-only. "mm- skip this turn" should
    # forward unchanged so it doesn't accidentally silence user prose.
    reg, calls, _ = _make_registry()
    assert reg.dispatch("mm- something else") == ("forward", None)
    assert calls == []


def test_mm_without_sid_returns_error() -> None:
    reg, calls, _ = _make_registry(sid=None)
    verdict, reply = reg.dispatch("mm-")
    assert verdict == "handled"
    assert reply is not None
    assert "无会话" in reply
    assert calls == []


def test_mm_literal_trims_whitespace() -> None:
    reg, calls, _ = _make_registry()
    verdict, _ = reg.dispatch("  mm-  ")
    assert verdict == "handled"
    assert calls[0][0] == "manual_skip"


def test_mm_default_writer_is_noop() -> None:
    # Default CommandContext (no audit_writer wired) must still handle mm- /
    # mm+ without raising — bridge runs even when marrow is offline.
    state = BridgeState(session_id="sid-x")
    ctx = CommandContext(
        state=state,
        swap_provider=lambda *_a, **_kw: None,
        close_provider=lambda: None,
        forget_session=lambda: None,
    )
    reg = Registry(ctx)
    verdict, reply = reg.dispatch("mm-")
    assert verdict == "handled"
    assert reply is not None
