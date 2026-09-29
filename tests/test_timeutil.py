"""[core].timezone drives local time on every channel, not the OS zone."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

import synapse_core.timeutil as timeutil
from synapse_core.anchor import time_anchor
from synapse_core.debounce import InboundBuffer
from synapse_core.providers.mock import EchoProvider
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_wx.config import Config as WxConfig
from synapse_wx.loop import MainLoop

INSTANT = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc).timestamp()
MEL = "[time: 2026-01-01 Thu 14:00]"
NYC = "[time: 2025-12-31 Wed 22:00]"


@pytest.fixture
def os_tz_new_york():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.fixture
def frozen(monkeypatch):
    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(INSTANT, tz)

    monkeypatch.setattr(timeutil, "datetime", _Fixed)


@pytest.fixture(autouse=True)
def _reset_process_zone(monkeypatch):
    monkeypatch.setattr(timeutil, "_configured", None)


class _FakeILink:
    def __init__(self, batches):
        self._batches = list(batches)
        self.sent = []

    def poll_messages(self):
        return self._batches.pop(0) if self._batches else []

    def send_text(self, to, ctx, text, **_):
        self.sent.append(text)
        return True

    @staticmethod
    def extract_text(msg):
        return msg.get("text", "")


class _RecordingProvider(EchoProvider):
    def __init__(self):
        super().__init__()
        self.prompts = []

    def send(self, msg):
        self.prompts.append(msg)
        super().send(msg)


def _wx_loop(tmp_path, cfg, ilink=None, clock=lambda: 1000.0):
    return MainLoop(
        ilink=ilink or _FakeILink([]),
        provider_factory=EchoProvider,
        state=BridgeState(),
        sessions=SessionTracker(state_path=tmp_path / "sessions.json"),
        buffer=InboundBuffer(clock=clock),
        clock=clock,
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        cfg=cfg,
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
    )


def test_resolve_tz_empty_and_unknown_mean_os_zone():
    assert timeutil.resolve_tz("") is None
    assert timeutil.resolve_tz("  ") is None
    assert timeutil.resolve_tz("Not/AZone") is None
    assert timeutil.resolve_tz("Australia/Melbourne") is not None


def test_wx_anchor_follows_configured_zone_not_os(tmp_path, os_tz_new_york, frozen):
    loop = _wx_loop(tmp_path, WxConfig(timezone="Australia/Melbourne"))
    assert time_anchor(loop._wallclock(), 0.0) == MEL


def test_wx_anchor_empty_zone_follows_os(tmp_path, os_tz_new_york, frozen):
    loop = _wx_loop(tmp_path, WxConfig(timezone=""))
    assert time_anchor(loop._wallclock(), 0.0) == NYC


def test_wx_anchor_without_cfg_uses_process_zone(tmp_path, os_tz_new_york, frozen):
    timeutil.configure("Australia/Melbourne")
    loop = _wx_loop(tmp_path, None)
    assert time_anchor(loop._wallclock(), 0.0) == MEL


def test_wx_flush_prompt_carries_configured_anchor(
        tmp_path, monkeypatch, os_tz_new_york, frozen):
    monkeypatch.setattr("synapse_wx.loop.get_session_created_at", lambda *_a: None)
    now = [1000.0]
    ilink = _FakeILink([[{"from_wxid": "u", "context_token": "c", "text": "hi"}]])
    loop = _wx_loop(tmp_path, WxConfig(timezone="Australia/Melbourne"),
                    ilink=ilink, clock=lambda: now[0])
    provider = _RecordingProvider()
    provider.spawn()
    loop._provider = provider
    loop.tick()
    now[0] += 60.0
    loop.maybe_flush()
    assert provider.prompts and provider.prompts[0].startswith("[time: 2026-01-01 Thu 14:00")


def test_tg_and_wx_render_the_same_local_time(tmp_path, os_tz_new_york, frozen):
    tg = TgLoop(TgConfig(data_dir=tmp_path / "tg", timezone="Australia/Melbourne",
                         marrow_db=str(tmp_path / "marrow.db")))
    wx = _wx_loop(tmp_path, WxConfig(timezone="Australia/Melbourne"))
    assert tg._local_hhmm_plus() == wx._wallclock().strftime("%H:%M") == "14:00"


def test_to_local_reads_naive_as_configured_wall_time(os_tz_new_york):
    timeutil.configure("Australia/Melbourne")
    dt = timeutil.to_local(datetime(2026, 1, 1, 14, 0))
    assert dt.timestamp() == INSTANT


def test_from_timestamp_uses_configured_zone(os_tz_new_york):
    timeutil.configure("Australia/Melbourne")
    assert timeutil.from_timestamp(INSTANT).strftime("%Y-%m-%d %H:%M") == "2026-01-01 14:00"
    timeutil.configure("")
    assert timeutil.from_timestamp(INSTANT).strftime("%Y-%m-%d %H:%M") == "2025-12-31 22:00"
