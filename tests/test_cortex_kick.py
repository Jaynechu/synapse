"""P6 watch + kick (synapse_core.cortex_kick): reply claims all armed watches on
a channel (single winner vs a concurrent timeout claim), timeout fires only when
events show no reply, each scenario kicks exactly once. All kick spawns are
mocked — never launch a real cortex.kick."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from synapse_core import cortex_kick

_OUTBOX_DDL = """
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  from_sid TEXT, from_channel TEXT,
  target TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', sent_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  watch_reply INTEGER NOT NULL DEFAULT 0,
  watch_timeout_min INTEGER, watch_state TEXT,
  replied_at TEXT, reply_text TEXT, receipt_seen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL DEFAULT 's', timestamp TEXT NOT NULL,
  role TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', channel TEXT
);
"""


def _db(tmp_path: Path) -> str:
    p = tmp_path / "marrow.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(_OUTBOX_DDL)
    conn.commit()
    conn.close()
    return str(p)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sent_watch(db, target="tg", *, watch_reply=1, timeout_min=None,
                sent_at=None, state="armed") -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO outbox (target, body, status, sent_at, watch_reply,"
        " watch_timeout_min, watch_state) VALUES (?, 'hi', 'sent', ?, ?, ?, ?)",
        (target, sent_at, watch_reply, timeout_min, state),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _user_event(db, channel, ts):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO events (timestamp, role, channel) VALUES (?, 'user', ?)",
        (ts, channel))
    conn.commit()
    conn.close()


def _state(db, rid) -> str:
    conn = sqlite3.connect(db)
    v = conn.execute("SELECT watch_state FROM outbox WHERE id=?", (rid,)).fetchone()[0]
    conn.close()
    return v


@pytest.fixture
def kicks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cortex_kick, "kick",
        lambda cmd, kind, **kw: calls.append({"kind": kind, **kw}) or True)
    return calls


# ── reply claim ───────────────────────────────────────────────────────────

def test_reply_claims_all_armed_on_channel(tmp_path):
    db = _db(tmp_path)
    a = _sent_watch(db, "tg")
    b = _sent_watch(db, "tg")
    ids = cortex_kick.claim_reply(db, "tg")
    assert set(ids) == {a, b}
    assert _state(db, a) == "fired" and _state(db, b) == "fired"


def test_reply_ignores_other_channel_and_unarmed(tmp_path):
    db = _db(tmp_path)
    _sent_watch(db, "wx")                       # other channel
    _sent_watch(db, "tg", watch_reply=0)        # not a watch
    _sent_watch(db, "tg", state="fired")        # already fired
    assert cortex_kick.claim_reply(db, "tg") == []


def test_reply_second_call_no_double(tmp_path):
    db = _db(tmp_path)
    _sent_watch(db, "tg")
    assert cortex_kick.claim_reply(db, "tg")     # first wins
    assert cortex_kick.claim_reply(db, "tg") == []  # nothing left


# ── receipt stamp (P12) ───────────────────────────────────────────────────

def _sent_plain(db, target="tg", *, status="sent", replied_at=None, sent_at=None) -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO outbox (target, body, status, replied_at, sent_at)"
        " VALUES (?, 'hi', ?, ?, ?)",
        (target, status, replied_at, sent_at))
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _receipt(db, rid):
    conn = sqlite3.connect(db)
    r = conn.execute(
        "SELECT replied_at, reply_text FROM outbox WHERE id=?", (rid,)).fetchone()
    conn.close()
    return r


def test_stamp_receipts_stamps_all_unreplied_sent_on_channel(tmp_path):
    db = _db(tmp_path)
    a = _sent_plain(db, "tg")                 # non-watch sent
    b = _sent_watch(db, "tg")                 # watch sent
    n = cortex_kick.stamp_receipts(db, "tg", "hey love")
    assert n == 2
    for rid in (a, b):
        rep = _receipt(db, rid)
        assert rep[0] and rep[1] == "hey love"


def test_stamp_receipts_channel_and_status_scoped(tmp_path):
    db = _db(tmp_path)
    other = _sent_plain(db, "wx")             # other channel
    pending = _sent_plain(db, "tg", status="pending")  # not yet sent
    assert cortex_kick.stamp_receipts(db, "tg", "hi") == 0
    assert _receipt(db, other)[0] is None
    assert _receipt(db, pending)[0] is None


def test_stamp_receipts_only_unreplied(tmp_path):
    db = _db(tmp_path)
    already = _sent_plain(db, "tg", replied_at="2026-07-17T00:00:00Z")
    fresh = _sent_plain(db, "tg")
    assert cortex_kick.stamp_receipts(db, "tg", "new text") == 1
    assert _receipt(db, already)[1] is None   # earlier receipt untouched
    assert _receipt(db, fresh)[1] == "new text"


def test_stamp_receipts_truncates(tmp_path):
    db = _db(tmp_path)
    rid = _sent_plain(db, "tg")
    cortex_kick.stamp_receipts(db, "tg", "x" * 500, text_chars=120)
    assert _receipt(db, rid)[1] == "x" * 120


def test_stamp_receipts_never_raises_on_missing_db(tmp_path):
    assert cortex_kick.stamp_receipts(str(tmp_path / "nope.db"), "tg", "hi") == 0
    assert cortex_kick.stamp_receipts(None, "tg", "hi") == 0


# ── receipt stamp time bound (F1) ─────────────────────────────────────────

def test_stamp_receipts_skips_note_sent_after_inbound(tmp_path):
    # Same-poll batch: cortex sent a note AFTER her earlier inbound message's
    # native timestamp. Must NOT stamp it as if she'd already replied.
    db = _db(tmp_path)
    note = _sent_plain(db, "tg", sent_at="2026-07-17T10:05:00Z")
    n = cortex_kick.stamp_receipts(
        db, "tg", "hey love", inbound_at="2026-07-17T10:00:00Z")
    assert n == 0
    assert _receipt(db, note)[0] is None


def test_stamp_receipts_stamps_note_sent_before_inbound(tmp_path):
    # Genuine later reply: the note went out before her inbound message.
    db = _db(tmp_path)
    note = _sent_plain(db, "tg", sent_at="2026-07-17T09:55:00Z")
    n = cortex_kick.stamp_receipts(
        db, "tg", "hey love", inbound_at="2026-07-17T10:00:00Z")
    assert n == 1
    assert _receipt(db, note)[1] == "hey love"


def test_stamp_receipts_boundary_sent_at_equals_inbound_at(tmp_path):
    db = _db(tmp_path)
    note = _sent_plain(db, "tg", sent_at="2026-07-17T10:00:00Z")
    n = cortex_kick.stamp_receipts(
        db, "tg", "hi", inbound_at="2026-07-17T10:00:00Z")
    assert n == 1


def test_stamp_receipts_no_inbound_at_falls_back_to_unbounded(tmp_path):
    # Caller with no native timestamp (legacy/defensive path) keeps old
    # unconditional behaviour rather than silently stamping nothing.
    db = _db(tmp_path)
    note = _sent_plain(db, "tg", sent_at="2026-07-17T10:05:00Z")
    n = cortex_kick.stamp_receipts(db, "tg", "hi")
    assert n == 1
    assert _receipt(db, note)[1] == "hi"


# ── timeout claim ─────────────────────────────────────────────────────────

def test_timeout_fires_when_no_reply(tmp_path):
    db = _db(tmp_path)
    past = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    rid = _sent_watch(db, "tg", timeout_min=10, sent_at=past)
    won = cortex_kick.claim_timeouts(db, "tg")
    assert [w["id"] for w in won] == [rid]
    assert _state(db, rid) == "fired"


def test_timeout_suppressed_when_reply_in_events(tmp_path):
    db = _db(tmp_path)
    sent = datetime.now(timezone.utc) - timedelta(minutes=30)
    rid = _sent_watch(db, "tg", timeout_min=10, sent_at=_iso(sent))
    _user_event(db, "tg", _iso(sent + timedelta(minutes=2)))  # she replied
    assert cortex_kick.claim_timeouts(db, "tg") == []
    assert _state(db, rid) == "satisfied"       # retired, not re-polled


def test_timeout_satisfied_claimed_once(tmp_path):
    db = _db(tmp_path)
    sent = datetime.now(timezone.utc) - timedelta(minutes=30)
    rid = _sent_watch(db, "tg", timeout_min=10, sent_at=_iso(sent))
    _user_event(db, "tg", _iso(sent + timedelta(minutes=2)))  # she replied
    assert cortex_kick.claim_timeouts(db, "tg") == []
    assert _state(db, rid) == "satisfied"
    # A satisfied row is no longer armed, so the next poll skips it entirely.
    assert cortex_kick.claim_timeouts(db, "tg") == []
    assert _state(db, rid) == "satisfied"


def test_timeout_not_yet_elapsed(tmp_path):
    db = _db(tmp_path)
    recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=2))
    _sent_watch(db, "tg", timeout_min=10, sent_at=recent)
    assert cortex_kick.claim_timeouts(db, "tg") == []


def test_timeout_needs_timeout_min(tmp_path):
    db = _db(tmp_path)
    past = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    _sent_watch(db, "tg", timeout_min=None, sent_at=past)  # watch_reply only
    assert cortex_kick.claim_timeouts(db, "tg") == []


# ── reply-vs-timeout single winner ────────────────────────────────────────

def test_reply_then_timeout_single_winner(tmp_path):
    db = _db(tmp_path)
    past = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    rid = _sent_watch(db, "tg", timeout_min=10, sent_at=past)
    assert cortex_kick.claim_reply(db, "tg") == [rid]   # reply wins first
    assert cortex_kick.claim_timeouts(db, "tg") == []   # timeout finds nothing
    assert _state(db, rid) == "fired"


# ── kick spawn: exactly once, mocked ──────────────────────────────────────

def test_kick_no_cmd_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(cortex_kick, "_warned_no_cmd", False)
    assert cortex_kick.kick(None, "reply") is False   # no cmd -> no spawn


def test_kick_spawns_with_kind_and_ids(monkeypatch):
    captured = {}
    class _P:
        def __init__(self, argv, **kw): captured["argv"] = argv
    monkeypatch.setattr(cortex_kick.subprocess, "Popen", _P)
    assert cortex_kick.kick(["py", "-m", "cortex.kick"], "timeout",
                            note_id=5, minutes=30)
    assert captured["argv"] == [
        "py", "-m", "cortex.kick", "--kind", "timeout",
        "--note-id", "5", "--minutes", "30"]


def test_kick_reply_carries_truncated_text(monkeypatch):
    captured = {}
    class _P:
        def __init__(self, argv, **kw): captured["argv"] = argv
    monkeypatch.setattr(cortex_kick.subprocess, "Popen", _P)
    assert cortex_kick.kick(["py", "-m", "cortex.kick"], "reply",
                            note_id=7, text="x" * 500, text_chars=200)
    argv = captured["argv"]
    assert argv[:6] == [
        "py", "-m", "cortex.kick", "--kind", "reply", "--note-id"]
    assert "--text" in argv
    sent_text = argv[argv.index("--text") + 1]
    assert sent_text == "x" * 200          # truncated to text_chars


# ── night flag / morning_start readers ────────────────────────────────────

def test_night_mode(tmp_path):
    p = tmp_path / "wake_state.json"
    p.write_text(json.dumps({"mode": "night"}))
    assert cortex_kick.night_mode(str(p)) is True
    p.write_text(json.dumps({"awake": True}))
    assert cortex_kick.night_mode(str(p)) is False
    assert cortex_kick.night_mode(str(tmp_path / "absent.json")) is False


def test_past_morning_start():
    assert cortex_kick.past_morning_start("00:00", "Australia/Melbourne") is True
    assert cortex_kick.past_morning_start("23:59", "Australia/Melbourne") is False
