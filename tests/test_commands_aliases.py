"""Tests for synapse_core.commands.aliases."""

from __future__ import annotations

from synapse_core.commands.aliases import (
    MODEL_ALIASES,
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
    assert resolve_model("sonnet") == "sonnet"


def test_resolve_alias_opus_stays_floating() -> None:
    # cc resolves the bare alias to the latest opus — no pinned id here, so a
    # new release needs no table edit.
    assert resolve_model("opus") == "opus"


def test_resolve_alias_5o_and_5f() -> None:
    assert resolve_model("5o") == "opus"
    assert resolve_model("5f") == "fable"


def test_resolve_alias_haiku() -> None:
    assert resolve_model("haiku") == "haiku"


def test_resolve_alias_fable() -> None:
    assert resolve_model("5") == "fable"
    assert resolve_model("fable") == "fable"


def test_no_alias_target_is_a_pinned_current_model() -> None:
    """Only the explicit legacy version keys may pin a canonical id."""
    floating = {k: v for k, v in MODEL_ALIASES.items() if not k.startswith("4.")}
    assert set(floating.values()) == {"opus", "sonnet", "haiku", "fable"}


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


def test_display_name_rule_based() -> None:
    assert display_name("claude-opus-5") == "Opus 5"
    assert display_name("claude-opus-4-6") == "Opus 4.6"
    assert display_name("claude-fable-5") == "Fable 5"
    assert display_name("claude-haiku-4-5-20251001") == "Haiku 4.5"


def test_display_name_bare_alias() -> None:
    # /model opus stores the floating alias; /info must still read cleanly.
    assert display_name("opus") == "Opus"
    assert display_name("sonnet") == "Sonnet"
    assert display_name("haiku") == "Haiku"


def test_display_name_future_model_needs_no_table_edit() -> None:
    assert display_name("claude-opus-6") == "Opus 6"
    assert display_name("claude-future-9") == "Future 9"


def test_display_name_with_context_suffix() -> None:
    # The 1M-context variant must surface as "[1M]" in /info.
    assert display_name("claude-opus-4-7[1m]") == "Opus 4.7 [1M]"
    assert display_name("claude-opus-4-8[1m]") == "Opus 4.8 [1M]"
    assert display_name("claude-opus-4-7[200k]") == "Opus 4.7 [200K]"
    assert display_name("opus[1m]") == "Opus [1M]"


def test_display_name_none() -> None:
    # Bridge starts with no model known; show "?" not the misleading "default".
    assert display_name(None) == "?"
    assert display_name("") == "?"


def test_display_name_unparseable_passes_through() -> None:
    assert display_name("gpt-5o-mini") == "gpt-5o-mini"
    assert display_name("some_local_model") == "some_local_model"
    assert display_name("claude-") == "claude-"
