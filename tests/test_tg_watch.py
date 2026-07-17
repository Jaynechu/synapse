"""P6 tg bridge wiring: from-her gate drives reply + morning kicks; watch_timeout
runs in the outbox poll. cortex_kick is mocked — no real cortex.kick spawn."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from synapse_core import cortex_kick
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop

_DDL = """
CREATE TABLE outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  from_sid TEXT, from_channel TEXT, target TEXT NOT NULL, body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', sent_at TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, watch_reply INTEGER NOT NULL DEFAULT 0,
  watch_timeout_min INTEGER, watch_state TEXT
);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT DEFAULT 's',
  timestamp TEXT NOT NULL, role TEXT NOT NULL, content TEXT DEFAULT '', channel TEXT);
"""


def _db(tmp_path):
    p = tmp_path / "marrow.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    return str(p)


def _armed_reply(db, target="tg"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO outbox (target, body, status, sent_at, watch_reply, watch_state)"
        " VALUES (?, 'x', 'sent', '2026-07-17T00:00:00Z', 1, 'armed')", (target,))
    conn.commit()
    conn.close()


def _loop(tmp_path, db, chat_id=999, kick_cmd=("py", "-m", "cortex.kick"),
          wake_state_file="", morning="06:00"):
    cfg = TgConfig(
        data_dir=tmp_path / "tg-data", marrow_db=db, chat_id=chat_id,
        outbox_kick_cmd=list(kick_cmd), cortex_wake_state_file=wake_state_file,
        night_morning_start=morning,
    )
    return TgLoop(cfg)


@pytest.fixture
def kicks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cortex_kick, "kick",
        lambda cmd, kind, **kw: calls.append({"kind": kind, **kw}) or True)
    return calls


class _Ctx:
    def __init__(self, bot): self.bot = bot


class _Bot:
    def __init__(self): self.sent = []
    async def send_message(self, **kw):
        self.sent.append(kw)
        return type("M", (), {"message_id": len(self.sent)})()


def test_is_from_her(tmp_path):
    loop = _loop(tmp_path, _db(tmp_path), chat_id=999)
    assert loop._is_from_her(999) is True
    assert loop._is_from_her(1234) is False
    loop._cfg.chat_id = None
    assert loop._is_from_her(999) is False


def test_from_her_reply_kicks_once(tmp_path, kicks):
    db = _db(tmp_path)
    _armed_reply(db)
    loop = _loop(tmp_path, db, chat_id=999)
    loop._track(_Bot(), 999, user_id=5)
    assert [k["kind"] for k in kicks] == ["reply"]


def test_reply_kick_carries_text(tmp_path, kicks):
    db = _db(tmp_path)
    _armed_reply(db)
    loop = _loop(tmp_path, db, chat_id=999)
    loop._track(_Bot(), 999, user_id=5, text="miss you")
    assert [k["kind"] for k in kicks] == ["reply"]
    assert kicks[0]["text"] == "miss you"


def test_other_chat_no_kick(tmp_path, kicks):
    db = _db(tmp_path)
    _armed_reply(db)
    loop = _loop(tmp_path, db, chat_id=999)
    loop._track(_Bot(), 1234, user_id=5)          # not her chat
    assert kicks == []


def test_from_her_no_armed_no_kick(tmp_path, kicks):
    db = _db(tmp_path)                             # no armed watch
    loop = _loop(tmp_path, db, chat_id=999)
    loop._track(_Bot(), 999, user_id=5)
    assert kicks == []


def test_morning_kick_when_night_and_past_start(tmp_path, kicks):
    db = _db(tmp_path)
    ws = tmp_path / "wake_state.json"
    ws.write_text(json.dumps({"mode": "night"}))
    loop = _loop(tmp_path, db, chat_id=999, wake_state_file=str(ws), morning="00:00")
    loop._track(_Bot(), 999, user_id=5)
    assert [k["kind"] for k in kicks] == ["morning"]


def test_no_morning_kick_when_flag_absent(tmp_path, kicks):
    db = _db(tmp_path)
    ws = tmp_path / "wake_state.json"
    ws.write_text(json.dumps({"awake": True}))    # day, no night flag
    loop = _loop(tmp_path, db, chat_id=999, wake_state_file=str(ws), morning="00:00")
    loop._track(_Bot(), 999, user_id=5)
    assert kicks == []


def test_outbox_poll_runs_watch_timeout(tmp_path, monkeypatch):
    db = _db(tmp_path)
    called = {}
    def _fake_claim(d, ch):
        called["ch"] = ch
        return [{"id": 3, "minutes": 10}]
    monkeypatch.setattr(cortex_kick, "claim_timeouts", _fake_claim)
    fired = []
    monkeypatch.setattr(cortex_kick, "kick",
                        lambda cmd, kind, **kw: fired.append({"kind": kind, **kw}))
    loop = _loop(tmp_path, db, chat_id=999)
    asyncio.run(loop.outbox_poll(_Ctx(_Bot())))
    assert called["ch"] == "tg"
    assert fired == [{"kind": "timeout", "note_id": 3, "minutes": 10}]
