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
# Truncated opener with no matching closer: only happens at a stream cut.
# Only the trailing run of tool-XML-ish lines that follows it is leaked —
# real prose after the cut must survive.
_ORPHAN_OPENER_RE = re.compile(r"<(?:antml:)?invoke\b[^>]*>")
_PARAM_OPEN_RE = re.compile(r"<(?:antml:)?parameter\b[^>]*>")
_PARAM_CLOSE_RE = re.compile(r"</(?:antml:)?parameter>")
_TAG_ONLY_LINE_RE = re.compile(
    r"^(?:</?(?:antml:)?invoke\b[^>]*>|</?(?:antml:)?function_(?:calls|results)>)$"
)
_WRAPPER_TAG_RE = re.compile(
    r"</?(?:antml:)?function_(?:calls|results)>", re.IGNORECASE
)
_MULTI_NL_RE = re.compile(r"\n{3,}")


def _strip_orphan_invoke(text: str) -> str:
    """Drop an unclosed `<invoke>` and the leaked tool-XML lines after it,
    stopping at the first line of real prose so it is kept intact."""
    m = _ORPHAN_OPENER_RE.search(text)
    if not m:
        return text
    before = text[: m.start()]
    lines = text[m.end() :].split("\n")
    in_param = False
    keep_from = len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if in_param:
            if _PARAM_CLOSE_RE.search(line):
                in_param = False
            continue
        if stripped == "" or _TAG_ONLY_LINE_RE.match(stripped):
            continue
        if _PARAM_OPEN_RE.search(line):
            if not _PARAM_CLOSE_RE.search(line):
                in_param = True
            continue
        keep_from = i
        break
    return before + "\n".join(lines[keep_from:])


def strip_tool_xml(text: str) -> str:
    """Remove leaked tool-call XML from assistant text; return cleaned text."""
    fences: list[str] = []

    def _stash(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    stashed = _FENCE_RE.sub(_stash, text)
    stashed = _INVOKE_BLOCK_RE.sub("", stashed)
    stashed = _strip_orphan_invoke(stashed)
    stashed = _WRAPPER_TAG_RE.sub("", stashed)

    def _restore(m: re.Match) -> str:
        return fences[int(m.group(1))]

    result = re.sub(r"\x00FENCE(\d+)\x00", _restore, stashed)
    result = _MULTI_NL_RE.sub("\n\n", result)
    return result.strip()
