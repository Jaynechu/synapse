"""Model alias table for natural and slash routing."""

from __future__ import annotations

import re

MODEL_ALIASES: dict[str, str] = {
    # Opus aliases always pin the 1M-context variant — Lumi never wants the
    # 200k default. Sonnet/Haiku have no 1M tier; aliases stay base.
    "4.6": "claude-opus-4-6[1m]",
    "4.7": "claude-opus-4-7[1m]",
    "4.8": "claude-opus-4-8[1m]",
    "opus": "claude-opus-4-8[1m]",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
    "5": "claude-fable-5",
    "fable": "claude-fable-5",
}

# Aliases safe for bare-text natural matching (no slash prefix).
# Digit-only keys excluded — too easy to misfire in wx/tg chat.
NATURAL_ALIASES: set[str] = {k for k in MODEL_ALIASES if not k.replace(".", "").isdigit()}

MODEL_NAMES: dict[str, str] = {
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-8": "Opus 4.8",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "claude-fable-5": "Fable 5",
}

# Strip cc context-window suffix like "[1m]" / "[200k]" before MODEL_NAMES lookup,
# then re-append in uppercase so /info shows "Opus 4.7 [1M]" not "Opus 4.7".
_SUFFIX_RE = re.compile(r"^(?P<base>.+?)(?P<suffix>\[[^\]]+\])$")


def resolve_model(token: str) -> str | None:
    """Map alias or canonical id to canonical id. None if unrecognised.

    Alias side is case-insensitive (e.g. "Sonnet" == "sonnet"); canonical
    pass-through accepts any non-empty token (preserves "[1m]"-style suffix
    so the user can pin the 1M context variant via /model claude-opus-4-7[1m]).
    """
    if not token:
        return None
    lowered = token.strip().lower()
    if lowered in MODEL_ALIASES:
        return MODEL_ALIASES[lowered]
    stripped = token.strip()
    if stripped:
        return stripped
    return None


def display_name(model_id: str | None) -> str:
    """Pretty name; preserves any context-window suffix like [1M].

    Examples:
      "claude-opus-4-7[1m]"  -> "Opus 4.7 [1M]"
      "claude-opus-4-7"      -> "Opus 4.7"
      "claude-haiku-4-5-20251001" -> "Haiku 4.5"
      unknown id pass-through; None -> "?"
    """
    if not model_id:
        return "?"
    m = _SUFFIX_RE.match(model_id)
    if m:
        base = m.group("base")
        suffix = m.group("suffix").upper()
        return f"{MODEL_NAMES.get(base, base)} {suffix}"
    return MODEL_NAMES.get(model_id, model_id)
