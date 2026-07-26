"""A /model switch is the new default: it persists and survives restart.

Covers the saved-model > config-default > None fallback on both bridges, plus
/clear following the saved model instead of resetting to the config default.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from synapse_core import bridge_state_store
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.providers.mock import EchoProvider
from synapse_core.state import BridgeState
from synapse_tg.config import TgConfig
from synapse_tg.loop import TgLoop
from synapse_wx.config import Config as WxConfig


def _noop(*_a, **_k) -> None:
    return None


def _stub_tg_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """/model dispatch respawns a real ClaudeCodeProvider; swap in the
    in-memory EchoProvider so these tests exercise persistence, not spawn."""
    monkeypatch.setattr(TgLoop, "_make_provider", lambda self: EchoProvider())


def _wx_boot(state_path: Path, cfg: WxConfig) -> BridgeState:
    """Mirror of synapse_wx.__main__ boot seeding (saved model wins)."""
    state = BridgeState()
    for k, v in bridge_state_store.load(state_path).items():
        if hasattr(state, k):
            setattr(state, k, v)
    if not state.model:
        state.model = cfg.default_model or cfg.clear_default_model or None
    return state


# ── tg ──────────────────────────────────────────────────────────


def test_tg_model_switch_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_tg_provider(monkeypatch)
    cfg = TgConfig(data_dir=tmp_path / "tg-data", default_model="opus")
    loop = TgLoop(cfg)
    assert loop._state.model == "opus"  # config default seeds first boot

    loop._registry.dispatch("/model sonnet")
    assert loop._state.model == "sonnet"

    # Simulated restart: fresh loop reads the same state file.
    reborn = TgLoop(cfg)
    assert reborn._state.model == "sonnet"


def test_tg_config_default_only_seeds_unswitched_bridge(tmp_path: Path) -> None:
    cfg = TgConfig(data_dir=tmp_path / "tg-data")
    assert TgLoop(cfg)._state.model is None  # empty default → cc picks its own
    cfg2 = TgConfig(data_dir=tmp_path / "tg-data-2", default_model="opus")
    assert TgLoop(cfg2)._state.model == "opus"


def test_tg_clear_follows_saved_model_not_config_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_tg_provider(monkeypatch)
    cfg = TgConfig(data_dir=tmp_path / "tg-data", default_model="opus")
    loop = TgLoop(cfg)
    loop._registry.dispatch("/model haiku")
    loop._registry.dispatch("/clear")
    assert loop._state.model == "haiku"
    assert TgLoop(cfg)._state.model == "haiku"


# ── wx ──────────────────────────────────────────────────────────


def test_wx_model_switch_survives_restart(tmp_path: Path) -> None:
    p = tmp_path / "bridge_state.json"
    cfg = WxConfig(default_model="opus")
    state = _wx_boot(p, cfg)
    assert state.model == "opus"

    ctx = CommandContext(
        state=state,
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
        persist_state=lambda: bridge_state_store.save(p, asdict(state)),
    )
    Registry(ctx).dispatch("/model 5f")
    assert state.model == "fable"

    assert _wx_boot(p, cfg).model == "fable"


def test_wx_boot_falls_back_to_config_default(tmp_path: Path) -> None:
    p = tmp_path / "bridge_state.json"
    assert _wx_boot(p, WxConfig()).model is None
    assert _wx_boot(p, WxConfig(default_model="opus")).model == "opus"


# ── /model persistence + alias passthrough ──────────────────────


def test_model_dispatch_fires_persist_immediately() -> None:
    calls: list[str | None] = []
    state = BridgeState()
    ctx = CommandContext(
        state=state,
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
        persist_state=lambda: calls.append(state.model),
    )
    Registry(ctx).dispatch("/model 5o")
    # Stored as the floating cc alias, and written to disk on the spot.
    assert state.model == "opus"
    assert calls == ["opus"]
