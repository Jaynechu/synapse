"""Resolved-model cache: /clear + /model acks name the model cc really used.

`state.model` holds the token handed to `cc --model` — usually a floating alias
("opus"). cc reports the concrete id in its system/init event; the loops record
`token -> real id` into `state.model_resolved`, and the acks read that map. No
provider/stdout read happens on the ack path, and the map is display-only.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from synapse_core import bridge_state_store
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.debounce import InboundBuffer
from synapse_core.sessionend.tracker import SessionTracker
from synapse_core.state import BridgeState
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_wx.loop import MainLoop

REAL_ID = "claude-opus-5[1m]"


class _Hooks:
    def __init__(self) -> None:
        self.swap_calls: list[tuple[str | None, str | None]] = []

    def swap(self, model: str | None, sid: str | None) -> None:
        self.swap_calls.append((model, sid))

    def noop(self, *_a, **_k) -> None:
        return None


def _reg(state: BridgeState) -> tuple[Registry, _Hooks]:
    hooks = _Hooks()
    ctx = CommandContext(
        state=state,
        swap_provider=hooks.swap,
        close_provider=hooks.noop,
        forget_session=hooks.noop,
    )
    return Registry(ctx), hooks


# ── ack rendering (read path) ─────────────────────────────────


def test_clear_ack_uses_cached_real_id() -> None:
    s = BridgeState(model="opus", model_resolved={"opus": REAL_ID})
    reg, _ = _reg(s)
    _, reply = reg.dispatch("/clear")
    assert reply == "新鸭上桌🦆 Opus 5 [1M][High]"


def test_clear_ack_falls_back_to_alias_on_miss() -> None:
    """Cache miss = today's output, byte for byte."""
    s = BridgeState(model="opus")
    reg, _ = _reg(s)
    _, reply = reg.dispatch("/clear")
    assert reply == "新鸭上桌🦆 Opus[High]"


def test_model_ack_uses_cached_real_id() -> None:
    s = BridgeState(model="sonnet", model_resolved={"opus": REAL_ID})
    reg, _ = _reg(s)
    _, reply = reg.dispatch("/model opus")
    assert reply == "🤖(Opus 5 [1M])上线中..."


def test_model_ack_falls_back_to_alias_on_miss() -> None:
    s = BridgeState(model="sonnet")
    reg, _ = _reg(s)
    _, reply = reg.dispatch("/model opus")
    assert reply == "🤖(Opus)上线中..."


def test_cache_never_leaks_into_state_model_or_spawn_arg() -> None:
    """Display-only: neither state.model nor the `--model` token changes."""
    s = BridgeState(model="sonnet", model_resolved={"opus": REAL_ID})
    reg, hooks = _reg(s)
    reg.dispatch("/model opus")
    assert s.model == "opus"
    reg.dispatch("/clear")
    assert s.model == "opus"
    assert hooks.swap_calls == [("opus", None), ("opus", None)]


# ── no provider read on the ack path ──────────────────────────


class _ExplodingProvider:
    """Any stdout read is a test failure — acks must not touch the pipe."""

    model = "opus"

    def __init__(self, *_a, **_k) -> None:
        self.alive = False
        self.model_actual: str | None = None
        self.usage_total: dict[str, int] = {}
        self.reads = 0

    def spawn(self, env: dict[str, str] | None = None) -> None:
        self.alive = True

    def _boom(self, *_a, **_k):
        self.reads += 1
        raise AssertionError("ack path performed a blocking provider read")

    recv = poll_line = _boom

    def cancel(self) -> None:
        self.alive = False

    def close(self) -> None:
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive


def _wx_loop(tmp_path: Path, state: BridgeState, persist=None) -> MainLoop:
    clock = lambda: 1000.0  # noqa: E731
    return MainLoop(
        ilink=object(),
        provider_factory=_ExplodingProvider,
        state=state,
        sessions=SessionTracker(state_path=tmp_path / "sessions.json"),
        buffer=InboundBuffer(clock=clock),
        clock=clock,
        wallclock=lambda: datetime(2026, 7, 26, 12, 0),
        sleeper=lambda _s: None,
        alert_dir=tmp_path / "alerts",
        channel="wx",
        last_active_path=tmp_path / "last_active.json",
        channel_label="CC-WX",
        persist_state=persist,
    )


def test_clear_and_model_acks_do_no_provider_reads(tmp_path: Path) -> None:
    state = BridgeState(model="opus", model_resolved={"opus": REAL_ID})
    loop = _wx_loop(tmp_path, state)
    hooks = _Hooks()
    reg = Registry(
        CommandContext(
            state=state,
            swap_provider=loop.swap_provider,
            close_provider=loop.close_provider,
            forget_session=hooks.noop,
        )
    )
    _, cleared = reg.dispatch("/clear")
    _, switched = reg.dispatch("/model sonnet")
    assert cleared == "新鸭上桌🦆 Opus 5 [1M][High]"
    assert switched == "🤖(Sonnet)上线中..."
    assert loop._provider.reads == 0


# ── write path ────────────────────────────────────────────────


def test_wx_init_event_caches_and_persists(tmp_path: Path) -> None:
    saved: list[dict] = []
    state = BridgeState(model="opus")
    loop = _wx_loop(tmp_path, state, persist=lambda: saved.append(dict(state.model_resolved)))
    loop._provider = _ExplodingProvider()
    loop._apply_init_event({"type": "system", "subtype": "init", "model": REAL_ID})
    assert state.model_resolved == {"opus": REAL_ID}
    assert saved == [{"opus": REAL_ID}]
    # Same id again: no rewrite, no extra persist.
    loop._apply_init_event({"type": "system", "subtype": "init", "model": REAL_ID})
    assert saved == [{"opus": REAL_ID}]
    # cc ships a new generation for the same alias: overwrite + persist.
    loop._apply_init_event(
        {"type": "system", "subtype": "init", "model": "claude-opus-6[1m]"}
    )
    assert state.model_resolved == {"opus": "claude-opus-6[1m]"}
    assert saved[-1] == {"opus": "claude-opus-6[1m]"}
    assert state.model == "opus"


def test_tg_init_event_caches_and_persists(tmp_path: Path) -> None:
    cfg = TgConfig(data_dir=tmp_path / "tg-data", default_model="opus")
    loop = TgLoop(cfg)
    loop._provider = _ExplodingProvider()
    loop._handle_init_event({"type": "system", "subtype": "init", "model": REAL_ID})
    assert loop._state.model_resolved == {"opus": REAL_ID}
    on_disk = json.loads((cfg.data_dir / "bridge_state.json").read_text())
    assert on_disk["model_resolved"] == {"opus": REAL_ID}
    assert loop._state.model == "opus"


# ── persistence round-trip ────────────────────────────────────


def test_map_survives_state_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "bridge_state.json"
    state = BridgeState(model="opus", model_resolved={"opus": REAL_ID})
    bridge_state_store.save(p, asdict(state))
    reborn = BridgeState()
    for k, v in bridge_state_store.load(p).items():
        setattr(reborn, k, v)
    assert reborn.model_resolved == {"opus": REAL_ID}
    _, reply = _reg(reborn)[0].dispatch("/clear")
    assert reply == "新鸭上桌🦆 Opus 5 [1M][High]"


def test_malformed_map_on_disk_is_dropped(tmp_path: Path) -> None:
    p = tmp_path / "bridge_state.json"
    p.write_text(json.dumps({"model": "opus", "model_resolved": ["nope"]}))
    assert bridge_state_store.load(p)["model_resolved"] == {}
    p.write_text(json.dumps({"model_resolved": {"opus": 7, "sonnet": "x"}}))
    assert bridge_state_store.load(p)["model_resolved"] == {"sonnet": "x"}
