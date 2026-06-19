"""Split assistant text into Telegram-sized chunks (4096 char limit)."""

from __future__ import annotations

import re

_MEDIA_TAG_RE = re.compile(
    r'<(image|gif|video|file)\s+path="([^"]+)"\s*/?>',
    re.IGNORECASE,
)

# Sentence-end punctuation (CJK always; ASCII .!? gated on next char).
_SENTENCE_END = "。！？.!?"
_OPEN_BRACKETS = "「『（(【〔[《〈"
_CLOSE_BRACKETS = "」』）)】〕]》〉"
_CLOSING_TRAILERS = _CLOSE_BRACKETS + "\"'"

DEFAULT_LIMIT = 4096
_PARA_SOFT_CAP = 99999


def _is_sentence_end(line: str, i: int) -> bool:
    c = line[i]
    if c in "。！？":
        return True
    if c in ".!?":
        nxt = line[i + 1] if i + 1 < len(line) else ""
        return nxt == "" or nxt.isspace()
    return False


def _split_sentences(line: str) -> list[str]:
    """Sentence-split one line, respecting bracket depth."""
    out: list[str] = []
    depth = 0
    last = 0
    n = len(line)
    i = 0
    while i < n:
        c = line[i]
        if c in _OPEN_BRACKETS:
            depth += 1
            i += 1
            continue
        if c in _CLOSE_BRACKETS:
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and _is_sentence_end(line, i):
            end = i + 1
            while end < n and line[end] in _SENTENCE_END:
                end += 1
            while end < n and line[end] in _CLOSING_TRAILERS:
                end += 1
            chunk = line[last:end].strip()
            if chunk:
                out.append(chunk)
            last = end
            i = end
            continue
        i += 1
    tail = line[last:].strip()
    if tail:
        out.append(tail)
    return out


def _hard_cut(text: str, limit: int) -> list[str]:
    """Word-boundary cut, fallback to hard cut."""
    out: list[str] = []
    rest = text
    while rest:
        if len(rest) <= limit:
            out.append(rest)
            break
        window = rest[:limit]
        cut = limit
        for i in range(len(window) - 1, -1, -1):
            if window[i].isspace():
                cut = i
                break
        chunk = rest[:cut].rstrip()
        if chunk:
            out.append(chunk)
        rest = rest[cut:].lstrip()
    return [p for p in out if p]


def _split_long_line(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    sentences: list[str] = []
    for s in _split_sentences(line):
        if len(s) <= limit:
            sentences.append(s)
        else:
            sentences.extend(_hard_cut(s, limit))
    # accrete sentences up to limit
    out: list[str] = []
    buf = ""
    for s in sentences:
        if not buf:
            buf = s
            continue
        sep = " " if (buf and not buf[-1].isspace() and not s[0].isspace()) else ""
        if len(buf) + len(sep) + len(s) <= limit:
            buf = buf + sep + s
        else:
            out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return out


def split_for_tg(text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Split assistant text into Telegram messages (<= limit chars each).

    Double-newline boundaries first (paragraph = one bubble), then sentence
    boundaries on overflow, hard cut last.
    """
    if not text:
        return []
    soft = min(_PARA_SOFT_CAP, limit)
    chunks: list[str] = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= soft:
            chunks.append(para)
            continue
        # Over soft cap — split on internal \n, merge lines up to soft cap
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        buf = ""
        for ln in lines:
            if not buf:
                buf = ln
                continue
            candidate = buf + "\n" + ln
            if len(candidate) <= soft:
                buf = candidate
            else:
                chunks.extend(_split_long_line(buf, soft))
                buf = ln
        if buf:
            chunks.extend(_split_long_line(buf, soft))
    return [c for c in chunks if c]


def split_for_tg_typed(
    text: str, limit: int = DEFAULT_LIMIT
) -> list[dict[str, str]]:
    """Split text with embedded media tags into typed bubbles.

    Returns list of {"kind": "text"/"image"/"gif"/"video"/"file", "text"/"path": ...}.
    """
    if not text:
        return []

    bubbles: list[dict[str, str]] = []
    last_end = 0

    for m in _MEDIA_TAG_RE.finditer(text):
        before = text[last_end : m.start()].strip()
        if before:
            for chunk in split_for_tg(before, limit):
                bubbles.append({"kind": "text", "text": chunk})
        bubbles.append({"kind": m.group(1).lower(), "path": m.group(2)})
        last_end = m.end()

    tail = text[last_end:].strip()
    if tail:
        for chunk in split_for_tg(tail, limit):
            bubbles.append({"kind": "text", "text": chunk})

    if not bubbles:
        return [{"kind": "text", "text": c} for c in split_for_tg(text, limit)]

    return bubbles
