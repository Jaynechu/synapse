"""P6 wx bridge wiring: from-her gate (from_wxid == target) drives reply +
morning kicks; watch_timeout runs in _outbox_scan. cortex_kick is mocked — no
real cortex.kick spawn."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from synapse_core import cortex_kick
from synapse_core.debounce import InboundBuffer
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_wx.config import Config
from synapse_wx.loop import MainLoop

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


class FakeClock:
    def __init__(self, start=1000.0): self.now = start
    def __call__(self): return self.now
    def advance(self, s): self.now += s


class FakeILink:
    def __init__(self, msgs=None): self._msgs = msgs or []
    def poll_messages(self): return self._msgs
    def send_text(self, to, ctx, text, **_): return True
    @staticmethod
    def extract_text(msg): return msg.get("text", "")


def _db(tmp_path):
    p = tmp_path / "marrow.db"
    conn = sqlite3.connect(str(p))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    return str(p)


def _armed_reply(db, target="wx"):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO outbox (target, body, status, sent_at, watch_reply, watch_state)"
        " VALUES (?, 'x', 'sent', '2026-07-17T00:00:00Z', 1, 'armed')", (target,))
    conn.commit()
    conn.close()


def _loop(tmp_path, db, ilink=None, *, target_wxid="wxid_her",
          kick_cmd=("py", "-m", "cortex.kick"), wake_state_file="", morning="06:00"):
    if ilink is None:
        ilink = FakeILink()
    clock = FakeClock()
    cfg = Config(
        marrow_db_path=db, target_wxid=target_wxid,
        outbox_kick_cmd=list(kick_cmd), cortex_wake_state_file=wake_state_file,
        night_morning_start=morning,
    )
    loop = MainLoop(
        ilink=ilink, provider_factory=EchoProvider, state=BridgeState(),
        sessions=SessionTracker(state_path=tmp_path / "sessions.json"),
        idle_loop=None, buffer=InboundBuffer(clock=clock),
        poll_interval_sec=0.01, clock=clock,
        wallclock=lambda: datetime(2026, 7, 17, 12, 0),
        sleeper=lambda _s: None, alert_dir=tmp_path / "alerts", cfg=cfg,
        channel="wx", last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX", alerts=None,
    )
    return loop, clock


@pytest.fixture
def kicks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cortex_kick, "kick",
        lambda cmd, kind, **kw: calls.append({"kind": kind, **kw}) or True)
    return calls


def test_is_from_her(tmp_path):
    loop, _ = _loop(tmp_path, _db(tmp_path), FakeILink())
    assert loop._is_from_her("wxid_her") is True
    assert loop._is_from_her("wxid_other") is False
    assert loop._is_from_her("") is False


def test_from_her_reply_kicks_once(tmp_path, kicks):
    db = _db(tmp_path)
    _armed_reply(db)
    loop, _ = _loop(tmp_path, db)
    loop._inbound_from_her()
    assert [k["kind"] for k in kicks] == ["reply"]


def test_tick_from_her_triggers_kick(tmp_path, kicks):
    db = _db(tmp_path)
    _armed_reply(db)
    ilink = FakeILink(msgs=[{"from_wxid": "wxid_her", "text": "hi"}])
    loop, _ = _loop(tmp_path, db, ilink)
    loop.tick()
    assert any(k["kind"] == "reply" for k in kicks)


def test_tick_other_sender_no_kick(tmp_path, kicks):
    db = _db(tmp_path)
    _armed_reply(db)
    ilink = FakeILink(msgs=[{"from_wxid": "wxid_other", "text": "hi"}])
    loop, _ = _loop(tmp_path, db, ilink)
    loop.tick()
    assert kicks == []


def test_morning_kick_when_night_and_past_start(tmp_path, kicks):
    db = _db(tmp_path)
    ws = tmp_path / "wake_state.json"
    ws.write_text(json.dumps({"mode": "night"}))
    loop, _ = _loop(tmp_path, db, FakeILink(), wake_state_file=str(ws), morning="00:00")
    loop._inbound_from_her()
    assert [k["kind"] for k in kicks] == ["morning"]


def test_no_morning_kick_flag_absent(tmp_path, kicks):
    db = _db(tmp_path)
    ws = tmp_path / "wake_state.json"
    ws.write_text(json.dumps({"awake": True}))
    loop, _ = _loop(tmp_path, db, FakeILink(), wake_state_file=str(ws), morning="00:00")
    loop._inbound_from_her()
    assert kicks == []


def test_outbox_scan_runs_watch_timeout(tmp_path, monkeypatch):
    db = _db(tmp_path)
    seen = {}
    def _fake(d, ch):
        seen["ch"] = ch
        return [{"id": 8, "minutes": 15}]
    monkeypatch.setattr(cortex_kick, "claim_timeouts", _fake)
    fired = []
    monkeypatch.setattr(cortex_kick, "kick",
                        lambda cmd, kind, **kw: fired.append({"kind": kind, **kw}))
    loop, _ = _loop(tmp_path, db)
    loop._outbox_scan()
    assert seen["ch"] == "wx"
    assert fired == [{"kind": "timeout", "note_id": 8, "minutes": 15}]
