"""P4 tg outbound adapter: pending->sent, inline-retry exhaustion->failed+alert,
crash-orphan claimed row on startup->failed (NOT resent)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from synapse_tg import outbox
from synapse_tg.config import TgConfig, load_config
from synapse_tg.loop import TgLoop

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


class RecordingAlerts:
    def __init__(self) -> None:
        self.written: list[dict] = []

    def write(self, severity, kind, message, source="", *, fingerprint=None):
        self.written.append(
            {"severity": severity, "kind": kind, "message": message,
             "source": source, "fingerprint": fingerprint}
        )
        return Path("/dev/null")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return type("M", (), {"message_id": len(self.sent)})()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    async def fake_sleep(_sec):
        return None

    monkeypatch.setattr("synapse_tg.loop.asyncio.sleep", fake_sleep)


def _db(tmp_path: Path) -> str:
    p = tmp_path / "marrow.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(_OUTBOX_DDL)
    conn.commit()
    conn.close()
    return str(p)


def _insert(db: str, body: str, target: str = "tg", status: str = "pending") -> int:
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


def _loop(tmp_path: Path, db: str, chat_id=999, alerts=None, retry_max=3) -> TgLoop:
    cfg = TgConfig(
        data_dir=tmp_path / "tg-data",
        marrow_db=db,
        chat_id=chat_id,
        outbox_retry_max=retry_max,
    )
    return TgLoop(cfg, alerts=alerts)


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


# --- config ---------------------------------------------------------------

def test_config_outbox_defaults():
    cfg = TgConfig()
    assert cfg.chat_id is None
    assert cfg.outbox_poll_interval_s == 5.0
    assert cfg.outbox_retry_max == 3


def test_config_outbox_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[tg]\nchat_id = 42\n[outbox]\npoll_interval_s = 2\nretry_max = 5\n"
    )
    cfg = load_config(p)
    assert cfg.chat_id == 42
    assert cfg.outbox_poll_interval_s == 2.0
    assert cfg.outbox_retry_max == 5


# --- pending -> sent ------------------------------------------------------

def test_pending_delivered_and_marked_sent(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "hey from another session")
    loop = _loop(tmp_path, db)
    bot = FakeBot()

    asyncio.run(loop.outbox_poll(_Ctx(bot)))

    # Default note_prefix marks it as bridge-sent, distinct from her own chat.
    assert [m["text"] for m in bot.sent] == ["\U0001f4ee hey from another session"]
    assert bot.sent[0]["chat_id"] == 999
    r = _row(db, rid)
    assert r["status"] == "sent"
    assert r["sent_at"] is not None


def test_only_tg_target_claimed(tmp_path):
    db = _db(tmp_path)
    tg_id = _insert(db, "for tg", target="tg")
    wx_id = _insert(db, "for wx", target="wx")
    loop = _loop(tmp_path, db)
    bot = FakeBot()

    asyncio.run(loop.outbox_poll(_Ctx(bot)))

    assert _row(db, tg_id)["status"] == "sent"
    assert _row(db, wx_id)["status"] == "pending"  # untouched


def test_no_chat_id_noops(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "note")
    loop = _loop(tmp_path, db, chat_id=None)
    bot = FakeBot()

    asyncio.run(loop.outbox_poll(_Ctx(bot)))

    assert bot.sent == []
    assert _row(db, rid)["status"] == "pending"


# --- inline-retry exhaustion -> failed + alert ----------------------------

def test_retry_exhaustion_marks_failed_and_alerts(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "note")
    alerts = RecordingAlerts()
    loop = _loop(tmp_path, db, alerts=alerts, retry_max=3)

    class FailBot(FakeBot):
        async def send_message(self, **kwargs):
            raise RuntimeError("api down")

    bot = FailBot()
    asyncio.run(loop.outbox_poll(_Ctx(bot)))

    r = _row(db, rid)
    assert r["status"] == "failed"
    assert r["retry_count"] == 3  # retry_max attempts made
    assert len(alerts.written) == 1
    a = alerts.written[0]
    assert a["kind"] == "tg_outbox_failed"
    assert a["fingerprint"] == "tg.outbox_failed"
    assert a["severity"] == "warn"


# --- crash orphan: claimed row on startup -> failed, NOT resent -----------

def test_orphan_claimed_swept_to_failed_not_resent(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "orphan note", status="claimed")
    alerts = RecordingAlerts()
    loop = _loop(tmp_path, db, alerts=alerts)
    bot = FakeBot()

    loop.sweep_outbox_orphans()

    # Never resent.
    assert bot.sent == []
    r = _row(db, rid)
    assert r["status"] == "failed"
    assert len(alerts.written) == 1
    assert alerts.written[0]["kind"] == "tg_outbox_orphan"
    assert alerts.written[0]["fingerprint"] == "tg.outbox_orphan"

    # A subsequent poll must not pick it up (it is failed, not pending).
    asyncio.run(loop.outbox_poll(_Ctx(bot)))
    assert bot.sent == []


def test_orphan_sweep_only_touches_tg(tmp_path):
    db = _db(tmp_path)
    tg_id = _insert(db, "tg orphan", target="tg", status="claimed")
    wx_id = _insert(db, "wx orphan", target="wx", status="claimed")
    loop = _loop(tmp_path, db)

    loop.sweep_outbox_orphans()

    assert _row(db, tg_id)["status"] == "failed"
    assert _row(db, wx_id)["status"] == "claimed"  # untouched


# --- atomic claim: concurrent poll each row claimed once ------------------

def test_claim_is_atomic_single_winner(tmp_path):
    db = _db(tmp_path)
    _insert(db, "a")
    _insert(db, "b")

    first = outbox.claim_pending(db)
    second = outbox.claim_pending(db)

    assert {r["id"] for r in first} == {1, 2}
    assert second == []  # nothing left pending


# --- note_prefix ------------------------------------------------------------

def test_note_prefix_config_default_and_override(tmp_path):
    assert TgConfig().outbox_note_prefix == "\U0001f4ee "
    p = tmp_path / "config.toml"
    p.write_text('[tg]\nchat_id = 42\n[outbox]\nnote_prefix = ">> "\n')
    cfg = load_config(p)
    assert cfg.outbox_note_prefix == ">> "


def test_empty_note_prefix_disables(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "plain note")
    cfg = TgConfig(
        data_dir=tmp_path / "tg-data",
        marrow_db=db,
        chat_id=999,
        outbox_note_prefix="",
    )
    loop = TgLoop(cfg)
    bot = FakeBot()

    asyncio.run(loop.outbox_poll(_Ctx(bot)))

    assert [m["text"] for m in bot.sent] == ["plain note"]
    assert _row(db, rid)["status"] == "sent"


def test_note_prefix_not_duplicated_when_already_present(tmp_path):
    db = _db(tmp_path)
    rid = _insert(db, "\U0001f4ee already prefixed")
    loop = _loop(tmp_path, db)
    bot = FakeBot()

    asyncio.run(loop.outbox_poll(_Ctx(bot)))

    assert [m["text"] for m in bot.sent] == ["\U0001f4ee already prefixed"]
    assert _row(db, rid)["status"] == "sent"


# --- multi-bubble body ----------------------------------------------------

def test_long_body_split_into_bubbles(tmp_path):
    db = _db(tmp_path)
    body = "x" * 5000  # over the 4096 tg limit
    rid = _insert(db, body)
    loop = _loop(tmp_path, db)
    bot = FakeBot()

    asyncio.run(loop.outbox_poll(_Ctx(bot)))

    assert len(bot.sent) >= 2
    assert _row(db, rid)["status"] == "sent"
    # Prefix lands on the first bubble only.
    assert bot.sent[0]["text"].startswith("\U0001f4ee")
    assert "\U0001f4ee" not in bot.sent[1]["text"]
