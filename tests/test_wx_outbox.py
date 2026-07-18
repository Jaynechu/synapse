"""P5 wx outbound adapter: pending->sent, claim atomicity, inline-retry
exhaustion->failed+alert, crash-orphan claimed->failed (NOT resent), no-recipient
no-op, partial-chunk failure->failed+alert (no bubble resend).

Poll folds into MainLoop.tick (wx has no job_queue); delivery via ILink.send_text
which chunks + retries internally — retry_max here counts send_text CALLS.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from synapse_core.debounce import InboundBuffer
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx import outbox
from synapse_wx.config import Config, load_config
from synapse_wx.loop import MainLoop

_OUTBOX_DDL = """
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  from_sid TEXT,
  from_channel TEXT,
  target TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  sent_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  watch_reply INTEGER NOT NULL DEFAULT 0,
  watch_timeout_min INTEGER,
  watch_state TEXT
);
"""


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class FakeILink:
    """send_text returns True (all chunks sent). Records every call."""

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def poll_messages(self) -> list:
        return []

    def send_text(self, to, ctx, text, **_kw) -> bool:
        self.sent.append((to, ctx, text))
        return True

    @staticmethod
    def extract_text(msg: dict) -> str:
        return msg.get("text", "")


class RaiseILink(FakeILink):
    def send_text(self, to, ctx, text, **_kw) -> bool:
        raise RuntimeError("ilink down")


class PartialILink(FakeILink):
    """send_text returns False = a chunk rejected, later chunks abandoned."""

    def send_text(self, to, ctx, text, **_kw) -> bool:
        self.sent.append((to, ctx, text))
        return False


class RecordingAlerts:
    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, severity, kind, message, source="", *, fingerprint=None):
        self.written.append(
            {"severity": severity, "kind": kind, "message": message,
             "source": source, "fingerprint": fingerprint}
        )
        return Path("/dev/null")


def _db(tmp_path: Path) -> str:
    p = tmp_path / "marrow.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(_OUTBOX_DDL)
    conn.commit()
    conn.close()
    return str(p)


def _insert(db: str, body: str, target: str = "wx", status: str = "pending") -> int:
    conn = sqlite3.connect(db)
    cur = conn.execute(
        "INSERT INTO outbox (target, body, status) VALUES (?, ?, ?)",
        (target, body, status),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def _row(db: str, row_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM outbox WHERE id=?", (row_id,)).fetchone()
    conn.close()
    return r


def _loop(tmp_path, db, ilink, *, target_wxid="wxid_her", alerts=None, retry_max=3):
    clock = FakeClock()
    cfg = Config(
        marrow_db_path=db,
        target_wxid=target_wxid,
        outbox_retry_max=retry_max,
    )
    loop = MainLoop(
        ilink=ilink,
        provider_factory=EchoProvider,
        state=BridgeState(),
        sessions=SessionTracker(state_path=tmp_path / "sessions.json"),
        idle_loop=None,
        buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01,
        clock=clock,
        wallclock=lambda: datetime(2026, 7, 17, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        cfg=cfg,
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        alerts=alerts,
    )
    return loop, clock


# --- config ---------------------------------------------------------------

def test_config_outbox_defaults():
    cfg = Config()
    assert cfg.outbox_poll_interval_s == 5.0
    assert cfg.outbox_retry_max == 3
    assert cfg.target_wxid == ""


def test_config_outbox_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[user]\ntarget_wxid = \"wxid_x\"\n[outbox]\npoll_interval_s = 2\nretry_max = 5\n"
    )
    cfg = load_config(p)
    assert cfg.target_wxid == "wxid_x"
    assert cfg.outbox_poll_interval_s == 2.0
    assert cfg.outbox_retry_max == 5


# --- pending -> sent ------------------------------------------------------

def test_pending_delivered_and_marked_sent(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "hey from another session")
    ilink = FakeILink()
    loop, _ = _loop(tmp_path, db, ilink)

    loop._outbox_scan()

    # Default note_prefix marks it as bridge-sent; empty context_token; to = target_wxid.
    assert ilink.sent == [("wxid_her", "", "\U0001f4ee hey from another session")]
    r = _row(db, rid)
    assert r["status"] == "sent"
    assert r["sent_at"] is not None


def test_only_wx_target_claimed(tmp_path):
    db = _db(tmp_path)
    wx_id = _insert(db, "for wx", target="wx")
    tg_id = _insert(db, "for tg", target="tg")
    ilink = FakeILink()
    loop, _ = _loop(tmp_path, db, ilink)

    loop._outbox_scan()

    assert _row(db, wx_id)["status"] == "sent"
    assert _row(db, tg_id)["status"] == "pending"  # untouched


def test_delivered_via_tick_after_poll_ok(tmp_path):
    """Poll folds into tick(): a poll-ok drives one delivery."""
    db = _db(tmp_path)
    rid = _insert(db, "note via tick")
    ilink = FakeILink()
    loop, _ = _loop(tmp_path, db, ilink)

    loop.tick()

    assert ilink.sent == [("wxid_her", "", "\U0001f4ee note via tick")]
    assert _row(db, rid)["status"] == "sent"


def test_no_target_wxid_noops(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "note")
    ilink = FakeILink()
    loop, _ = _loop(tmp_path, db, ilink, target_wxid="")

    loop._outbox_scan()

    assert ilink.sent == []
    assert _row(db, rid)["status"] == "pending"


# --- scan cadence gate ----------------------------------------------------

def test_scan_respects_poll_interval(tmp_path):
    db = _db(tmp_path)
    _insert(db, "first")
    ilink = FakeILink()
    loop, clock = _loop(tmp_path, db, ilink)

    loop._outbox_scan()  # due immediately (last_scan=0)
    assert len(ilink.sent) == 1

    _insert(db, "second")
    loop._outbox_scan()  # within 5s window -> skipped
    assert len(ilink.sent) == 1

    clock.advance(6.0)
    loop._outbox_scan()  # window elapsed -> delivers
    assert len(ilink.sent) == 2


# --- inline-retry exhaustion -> failed + alert ----------------------------

def test_retry_exhaustion_marks_failed_and_alerts(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "note")
    alerts = RecordingAlerts()
    loop, _ = _loop(tmp_path, db, RaiseILink(), alerts=alerts, retry_max=3)

    loop._outbox_scan()

    r = _row(db, rid)
    assert r["status"] == "failed"
    assert r["retry_count"] == 3  # retry_max send_text CALLS made
    assert len(alerts.written) == 1
    a = alerts.written[0]
    assert a["kind"] == "wx_outbox_failed"
    assert a["fingerprint"] == "wx.outbox_failed"
    assert a["severity"] == "warn"


# --- partial-chunk failure -> failed + alert, NO resend -------------------

def test_partial_chunk_failure_marks_failed_no_resend(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "long body split into chunks")
    alerts = RecordingAlerts()
    ilink = PartialILink()
    loop, _ = _loop(tmp_path, db, ilink, alerts=alerts, retry_max=3)

    loop._outbox_scan()

    # send_text called exactly once — no whole-call retry after partial failure.
    assert len(ilink.sent) == 1
    r = _row(db, rid)
    assert r["status"] == "failed"
    assert r["retry_count"] == 1
    assert len(alerts.written) == 1
    assert alerts.written[0]["kind"] == "wx_outbox_failed"
    assert alerts.written[0]["fingerprint"] == "wx.outbox_failed"


# --- crash orphan: claimed row on startup -> failed, NOT resent -----------

def test_orphan_claimed_swept_to_failed_not_resent(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "orphan note", status="claimed")
    alerts = RecordingAlerts()
    ilink = FakeILink()
    loop, _ = _loop(tmp_path, db, ilink, alerts=alerts)

    loop.sweep_outbox_orphans()

    assert ilink.sent == []  # never resent
    r = _row(db, rid)
    assert r["status"] == "failed"
    assert len(alerts.written) == 1
    assert alerts.written[0]["kind"] == "wx_outbox_orphan"
    assert alerts.written[0]["fingerprint"] == "wx.outbox_orphan"

    # A subsequent scan must not pick it up (failed, not pending).
    loop._outbox_scan()
    assert ilink.sent == []


def test_orphan_sweep_only_touches_wx(tmp_path):
    db = _db(tmp_path)
    wx_id = _insert(db, "wx orphan", target="wx", status="claimed")
    tg_id = _insert(db, "tg orphan", target="tg", status="claimed")
    loop, _ = _loop(tmp_path, db, FakeILink())

    loop.sweep_outbox_orphans()

    assert _row(db, wx_id)["status"] == "failed"
    assert _row(db, tg_id)["status"] == "claimed"  # untouched


# --- note_prefix ------------------------------------------------------------

def test_note_prefix_config_default_and_override(tmp_path):
    assert Config().outbox_note_prefix == "\U0001f4ee "
    p = tmp_path / "config.toml"
    p.write_text('[user]\ntarget_wxid = "wxid_x"\n[outbox]\nnote_prefix = ">> "\n')
    cfg = load_config(p)
    assert cfg.outbox_note_prefix == ">> "


def test_empty_note_prefix_disables(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "plain note")
    ilink = FakeILink()
    loop, _ = _loop(tmp_path, db, ilink)
    loop._cfg.outbox_note_prefix = ""

    loop._outbox_scan()

    assert ilink.sent == [("wxid_her", "", "plain note")]
    assert _row(db, rid)["status"] == "sent"


def test_note_prefix_not_duplicated_when_already_present(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "\U0001f4ee already prefixed")
    ilink = FakeILink()
    loop, _ = _loop(tmp_path, db, ilink)

    loop._outbox_scan()

    assert ilink.sent == [("wxid_her", "", "\U0001f4ee already prefixed")]
    assert _row(db, rid)["status"] == "sent"


class ChunkingILink(FakeILink):
    """Mirrors ilink.client.send_text: splits at max_len like the real client,
    recording each chunk as a separate send call."""

    def send_text(self, to, ctx, text, max_len=20, **_kw) -> bool:
        while text:
            chunk, text = text[:max_len], text[max_len:]
            self.sent.append((to, ctx, chunk))
        return True


def test_note_prefix_only_on_first_chunk(tmp_path):
    db = _db(tmp_path)
    body = "x" * 50
    rid = _insert(db, body)
    ilink = ChunkingILink()
    loop, _ = _loop(tmp_path, db, ilink)

    loop._outbox_scan()

    assert len(ilink.sent) >= 2
    assert ilink.sent[0][2].startswith("\U0001f4ee ")
    assert not ilink.sent[1][2].startswith("\U0001f4ee ")
    assert _row(db, rid)["status"] == "sent"


# --- atomic claim: concurrent poll each row claimed once ------------------

def test_claim_is_atomic_single_winner(tmp_path):
    db = _db(tmp_path)
    _insert(db, "a")
    _insert(db, "b")

    first = outbox.claim_pending(db)
    second = outbox.claim_pending(db)

    assert {r["id"] for r in first} == {1, 2}
    assert second == []  # nothing left pending
