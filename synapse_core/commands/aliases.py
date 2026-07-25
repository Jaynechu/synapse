"""Model alias table for natural and slash routing."""

from __future__ import annotations

import re

MODEL_ALIASES: dict[str, str] = {
    # Legacy opus versions stay pinned to the 1M-context variant — those are
    # explicit "give me that old model" requests. Every other key maps to a cc
    # *alias* (opus/sonnet/haiku/fable) which cc resolves to the latest model
    # of that family, so a new release needs no edit here.
    "4.6": "claude-opus-4-6[1m]",
    "4.7": "claude-opus-4-7[1m]",
    "4.8": "claude-opus-4-8[1m]",
    "5": "fable",
    "5o": "opus",
    "5f": "fable",
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "fable": "fable",
}

# Aliases safe for bare-text natural matching (no slash prefix).
# Digit-only keys excluded — too easy to misfire in wx/tg chat.
NATURAL_ALIASES: set[str] = {k for k in MODEL_ALIASES if not k.isdigit()}

# Strip cc context-window suffix like "[1m]" / "[200k]" before prettifying,
# then re-append in uppercase so /info shows "Opus 4.7 [1M]" not "Opus 4.7".
_SUFFIX_RE = re.compile(r"^(?P<base>.+?)(?P<suffix>\[[^\]]+\])$")
_DATE_RE = re.compile(r"\d{8}")


def resolve_model(token: str) -> str | None:
    """Map alias or canonical id to the token handed to `cc --model`.

    Alias side is case-insensitive (e.g. "Sonnet" == "sonnet"); anything else
    passes through unchanged (preserves "[1m]"-style suffix so the user can
    pin the 1M context variant via /model claude-opus-4-7[1m]).
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


def _prettify(base: str) -> str:
    """Rule-based family/version formatting. Unparseable → unchanged."""
    parts = (base[len("claude-"):] if base.startswith("claude-") else base).split("-")
    if len(parts) > 1 and _DATE_RE.fullmatch(parts[-1]):
        parts = parts[:-1]
    if not parts[0].isalpha() or not all(p.isdigit() for p in parts[1:]):
        return base
    version = ".".join(parts[1:])
    return f"{parts[0].capitalize()} {version}" if version else parts[0].capitalize()


def display_name(model_id: str | None) -> str:
    """Pretty name; preserves any context-window suffix like [1M].

    Examples:
      "claude-opus-4-7[1m]"       -> "Opus 4.7 [1M]"
      "claude-opus-5"             -> "Opus 5"
      "claude-haiku-4-5-20251001" -> "Haiku 4.5"
      "opus"                      -> "Opus"
      unparseable id pass-through; None -> "?"
    """
    if not model_id:
        return "?"
    m = _SUFFIX_RE.match(model_id)
    if m:
        return f"{_prettify(m.group('base'))} {m.group('suffix').upper()}"
    return _prettify(model_id)
