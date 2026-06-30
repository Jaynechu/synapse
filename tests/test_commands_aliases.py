"""Tests for synapse_wx.commands.aliases."""

from __future__ import annotations

from synapse_core.commands.aliases import (
    MODEL_ALIASES,
    MODEL_NAMES,
    display_name,
    resolve_model,
)


def test_resolve_alias_46_pins_1m() -> None:
    assert resolve_model("4.6") == "claude-opus-4-6[1m]"


def test_resolve_alias_47_pins_1m() -> None:
    assert resolve_model("4.7") == "claude-opus-4-7[1m]"


def test_resolve_alias_48_pins_1m() -> None:
    assert resolve_model("4.8") == "claude-opus-4-8[1m]"


def test_resolve_alias_sonnet() -> None:
    assert resolve_model("sonnet") == MODEL_ALIASES["sonnet"]


def test_resolve_alias_opus_is_4_8_1m() -> None:
    assert resolve_model("opus") == MODEL_ALIASES["opus"]


def test_resolve_alias_haiku_dated() -> None:
    assert resolve_model("haiku") == MODEL_ALIASES["haiku"]


def test_resolve_alias_fable_5() -> None:
    assert resolve_model("5") == MODEL_ALIASES["fable"]
    assert resolve_model("fable") == MODEL_ALIASES["fable"]


def test_resolve_alias_case_insensitive() -> None:
    assert resolve_model("Sonnet") == MODEL_ALIASES["sonnet"]
    assert resolve_model("OPUS") == MODEL_ALIASES["opus"]


def test_resolve_canonical_pass_through() -> None:
    # Canonical ids return unchanged.
    assert resolve_model("claude-opus-4-7") == "claude-opus-4-7"


def test_resolve_canonical_with_suffix_pass_through() -> None:
    # cc accepts context-window-pinned ids like "[1m]"; bridge must not strip them.
    assert resolve_model("claude-opus-4-7[1m]") == "claude-opus-4-7[1m]"
    assert resolve_model("claude-opus-4-6[1m]") == "claude-opus-4-6[1m]"


def test_resolve_unknown_passes_through() -> None:
    # Non-alias non-empty tokens flow to cc, which validates.
    assert resolve_model("claude-future-9") == "claude-future-9"


def test_resolve_empty_returns_none() -> None:
    assert resolve_model("") is None
    assert resolve_model("   ") is None


def test_display_name_known() -> None:
    for model_id, expected in MODEL_NAMES.items():
        assert display_name(model_id) == expected


def test_display_name_fable() -> None:
    assert display_name("claude-fable-5") == MODEL_NAMES["claude-fable-5"]


def test_display_name_known_with_context_suffix() -> None:
    # The 1M-context variant must surface as "[1M]" in /info.
    assert display_name("claude-opus-4-7[1m]") == "Opus 4.7 [1M]"
    assert display_name("claude-opus-4-8[1m]") == "Opus 4.8 [1M]"
    assert display_name("claude-opus-4-7[200k]") == "Opus 4.7 [200K]"


def test_display_name_unknown_with_suffix_passes_through() -> None:
    assert display_name("claude-future-9[1m]") == "claude-future-9 [1M]"


def test_display_name_none() -> None:
    # Bridge starts with no model known; show "?" not the misleading "default".
    assert display_name(None) == "?"
    assert display_name("") == "?"


def test_display_name_unknown_falls_back_to_id() -> None:
    assert display_name("claude-future-9") == "claude-future-9"
