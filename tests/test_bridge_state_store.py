"""Tests for the persisted bridge_state file.

`effort_level` / `thinking_on` / `quote_on` survive bridge crashes;
everything else (incl. `model`) stays session-scoped.
"""

from __future__ import annotations

from pathlib import Path

from synapse_core import bridge_state_store
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.state import BridgeState


def _noop(*_a, **_k) -> None:
    return None


def test_bridge_state_persist_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "bridge_state.json"
    bridge_state_store.save(
        p,
        {
            "effort_level": "high",
            "thinking_on": True,
            "quote_on": True,
            # model is intentionally session-scoped — should be dropped.
            "model": "claude-opus-4-6[1m]",
            "session_id": "ignored",
        },
    )
    loaded = bridge_state_store.load(p)
    assert loaded == {
        "effort_level": "high",
        "thinking_on": True,
        "quote_on": True,
        "session_id": "ignored",
    }


def test_bridge_state_load_missing_returns_empty(tmp_path: Path) -> None:
    assert bridge_state_store.load(tmp_path / "nope.json") == {}


def test_bridge_state_load_malformed_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not valid", encoding="utf-8")
    assert bridge_state_store.load(p) == {}


def test_bridge_state_load_non_dict_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert bridge_state_store.load(p) == {}


def test_bridge_state_cc_cwd_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "bridge_state.json"
    bridge_state_store.save(p, {"cc_cwd": "/Users/Gabrielle/CC-Lab/marrow"})
    assert bridge_state_store.load(p) == {"cc_cwd": "/Users/Gabrielle/CC-Lab/marrow"}
    # Overwrite — confirms /cwd switch persists the new value.
    bridge_state_store.save(p, {"cc_cwd": "/Users/Gabrielle/Desktop/NY"})
    assert bridge_state_store.load(p) == {"cc_cwd": "/Users/Gabrielle/Desktop/NY"}


def test_bridge_state_save_drops_unknown_keys(tmp_path: Path) -> None:
    p = tmp_path / "bridge_state.json"
    bridge_state_store.save(
        p, {"thinking_on": True, "wild_card": 99, "model": "drop_me"}
    )
    loaded = bridge_state_store.load(p)
    assert "wild_card" not in loaded
    assert "model" not in loaded  # session-scoped
    assert loaded["thinking_on"] is True


# ── persist_state wiring through registry ───────────────────────


def test_effort_dispatch_fires_persist() -> None:
    calls: list[int] = []
    state = BridgeState()
    ctx = CommandContext(
        state=state,
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
        persist_state=lambda: calls.append(1),
    )
    Registry(ctx).dispatch("/effort low")
    assert state.effort_level == "low"
    assert len(calls) == 1


def test_thinking_dispatch_fires_persist() -> None:
    calls: list[int] = []
    state = BridgeState()
    ctx = CommandContext(
        state=state,
        swap_provider=_noop,
        close_provider=_noop,
        forget_session=_noop,
        persist_state=lambda: calls.append(1),
    )
    Registry(ctx).dispatch("/thinking on")
    assert state.thinking_on is True
    assert len(calls) == 1
