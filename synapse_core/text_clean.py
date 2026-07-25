"""Strip literal tool-call XML that leaks into assistant text blocks.

The provider sometimes emits a raw `<invoke>...</invoke>` (or a truncated
opener, on a streaming cut) as plain text instead of a real tool_use block.
This scrubs that leakage before a bubble is sent to the user, while leaving
fenced code blocks and media-protocol tags (`<image path="...">` etc.)
untouched.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INVOKE_BLOCK_RE = re.compile(
    r"<(?:antml:)?invoke\b[^>]*>.*?</(?:antml:)?invoke>", re.DOTALL
)
# Truncated opener with no matching closer: only happens at a stream cut,
# so everything from the opener to the end of the text is leaked XML.
_INVOKE_ORPHAN_RE = re.compile(r"<(?:antml:)?invoke\b[^>]*>.*\Z", re.DOTALL)
_WRAPPER_TAG_RE = re.compile(
    r"</?(?:antml:)?function_(?:calls|results)>", re.IGNORECASE
)
_MULTI_NL_RE = re.compile(r"\n{3,}")


def strip_tool_xml(text: str) -> str:
    """Remove leaked tool-call XML from assistant text; return cleaned text."""
    fences: list[str] = []

    def _stash(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    stashed = _FENCE_RE.sub(_stash, text)
    stashed = _INVOKE_BLOCK_RE.sub("", stashed)
    stashed = _INVOKE_ORPHAN_RE.sub("", stashed)
    stashed = _WRAPPER_TAG_RE.sub("", stashed)

    def _restore(m: re.Match) -> str:
        return fences[int(m.group(1))]

    result = re.sub(r"\x00FENCE(\d+)\x00", _restore, stashed)
    result = _MULTI_NL_RE.sub("\n\n", result)
    return result.strip()
