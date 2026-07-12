"""Outbound bubble split: shape assistant text into WeChat-sized chunks.

Rules (Phase B, v4 — paragraph-first):
- The source paragraph (= one `\\n`-delimited line) is the natural unit. A
  paragraph at or under ``hard_max`` ships as ONE bubble regardless of how
  many sentences sit inside — preserves natural cn chat rhythm (short
  emotional bursts, technical lines, English paragraphs, code lines).
- Only when a paragraph exceeds ``hard_max`` do we fall back to
  sentence-split (bracket-depth aware) and accrete sentences back up to
  ``hard_max`` per bubble.
- List items (lines starting with `-`, `·`, `*`) — one bubble per item,
  never split, never merged with siblings.
- Sentence-end inside an unclosed bracket / quote (`「『（(【〔[《〈`) is NOT
  a boundary — keeps `「过来！」` whole inside long paragraphs.
- Closing brackets / quotes (`」』）)】〕]》〉"'`) right after a sentence-end
  get absorbed onto the previous sentence.
- Commas are NOT a boundary. A single sentence over ``hard_max`` is
  word-boundary cut as a pathological fallback (code dumps, long URLs).
- buddy HTML comments stripped bridge-side; cc cli / statusline / buddy MCP
  untouched.
"""

from __future__ import annotations

import re

# ── markdown-to-plain ───────────────────────────────────────────────────────

_BUDDY_COMMENT = re.compile(r"<!--\s*buddy:.*?-->", re.DOTALL)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# C1 media tags: <image|file|gif|video path="...">  (also accepts /> and single quotes)
_MEDIA_TAG = re.compile(
    r"""<\s*(?P<kind>image|file|gif|video)\s+path\s*=\s*['"](?P<path>[^'"]+)['"]\s*/?\s*>""",
    re.IGNORECASE,
)
_MEDIA_KINDS = {"image", "file", "gif", "video"}
_FENCE_OPEN = re.compile(r"```\w*\n?")
_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*")
_BOLD_UNDER = re.compile(r"__(.+?)__")
_ITAL_STAR = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_ITAL_UNDER = re.compile(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)")
_BACKTICK = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_HRULE = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_BLANKS = re.compile(r"\n{3,}")

# ── splitting helpers ──────────────────────────────────────────────────────

# Sentence-end chars (CJK punct always counts; ASCII .!? gated on next-char
# inside `_is_sentence_end`).
_SENTENCE_END_CHARS = "。！？.!?"
# Brackets / quotes whose contents are protected from sentence splitting.
# Only directional CJK / ASCII brackets — non-directional `'"` are skipped
# (parity is ambiguous in mid-text); they only act as trailing absorbers.
_OPEN_BRACKETS = "「『（(【〔[《〈"
_CLOSE_BRACKETS = "」』）)】〕]》〉"
# Absorbed onto the previous sentence when they immediately follow a
# sentence-end punct (so `「过来！」` keeps the `」` instead of leaking
# into the next bubble).
_CLOSING_TRAILERS = _CLOSE_BRACKETS + "\"'"
# List item: optional leading whitespace, then `-` / `·` / `*` followed by space.
_LIST_ITEM = re.compile(r"^\s*[-·*]\s+")

# Paragraph ceiling. A paragraph (one source line) at or under this stays a
# single bubble; over this we fall back to sentence-split + accrete back up
# to the same ceiling. Tuned to keep bubbles within ~6-8 wx lines.
DEFAULT_HARD_MAX = 99999
MAX_WX_BUBBLES = 99
# When merging adjacent text bubbles to honor the outbound cap, never grow a
# merged bubble past this so the client's per-chunk 4000-char re-split (which
# would create extra messages and defeat the cap) is not triggered.
MERGE_CHAR_CEILING = 3900

# /thinking: emit cc's plaintext thinking as ONE wx bubble prefixed 🧠.
# Test-drive: no splitting; we want to see how wx renders a long single
# bubble (and whether iLink rejects it) before deciding on splitting.
_THINKING_PREFIX = "🧠"


def format_thinking_bubbles(text: str | None) -> list[str]:
    """Single bubble per turn: ``[🧠 <text>]``. No truncation, no splitting.

    Returns ``[]`` when there is nothing meaningful to show.
    """
    if not text:
        return []
    body = text.strip()
    if not body:
        return []
    return [f"{_THINKING_PREFIX}{body}"]


def md_to_plain(text: str) -> str:
    """Flatten markdown into WeChat-friendly plain text. Strips buddy comments."""
    text = _BUDDY_COMMENT.sub("", text)
    text = _HTML_COMMENT.sub("", text)
    text = _FENCE_OPEN.sub("", text)
    text = _BOLD_STAR.sub(r"\1", text)
    text = _BOLD_UNDER.sub(r"\1", text)
    text = _ITAL_STAR.sub(r"\1", text)
    text = _ITAL_UNDER.sub(r"\1", text)
    text = _BACKTICK.sub(r"\1", text)
    text = _LINK.sub(r"\1 (\2)", text)
    text = _HEADING.sub("", text)
    text = _HRULE.sub("--------", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


def _is_list_item(line: str) -> bool:
    """True if the line looks like a markdown bullet item (-, ·, *)."""
    return bool(_LIST_ITEM.match(line))


def _word_boundary_cut(rest: str, hard_max: int) -> int:
    """Return a cut offset ≤ hard_max preferring the last ASCII whitespace
    inside the window (so we never split an English word). Falls back to
    hard_max when no whitespace exists (pure CJK or "aaaa...")."""
    if len(rest) <= hard_max:
        return len(rest)
    window = rest[:hard_max]
    for i in range(len(window) - 1, -1, -1):
        if window[i].isspace():
            return i
    return hard_max


def _is_sentence_end(line: str, i: int) -> bool:
    """Boundary test at offset ``i``. CJK punct always; ASCII .!? only when
    followed by whitespace or end-of-string (so `e.g.` and URLs survive).
    """
    c = line[i]
    if c in "。！？":
        return True
    if c in ".!?":
        nxt = line[i + 1] if i + 1 < len(line) else ""
        return nxt == "" or nxt.isspace()
    return False


def _split_into_sentences(line: str) -> list[str]:
    """Sentence-split one line, respecting bracket depth.

    - Sentence-end inside an unclosed bracket / quote does NOT trigger a cut.
    - Consecutive sentence-end chars (``！！`` / ``？？？``) are folded into
      one boundary so they never spawn lone-punct bubbles.
    - Closing brackets / quotes immediately following the boundary are
      absorbed onto the preceding sentence.
    """
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
            if depth > 0:
                depth -= 1
            i += 1
            continue
        if depth == 0 and _is_sentence_end(line, i):
            end = i + 1
            while end < n and line[end] in _SENTENCE_END_CHARS:
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


def _is_cjk(ch: str) -> bool:
    """True for CJK ideographs + CJK / fullwidth punctuation."""
    if not ch:
        return False
    cp = ord(ch)
    return 0x3000 <= cp <= 0x9FFF or 0xFF00 <= cp <= 0xFFEF


def _needs_space(prev: str, nxt: str) -> bool:
    """Insert a single ASCII space when joining two non-CJK boundaries.

    CJK ↔ CJK or CJK ↔ ASCII joins stay tight (matches native typography).
    """
    if not prev or not nxt:
        return False
    return not _is_cjk(prev[-1]) and not _is_cjk(nxt[0])


def _merge_sentences(sentences: list[str], cap: int) -> list[str]:
    """Accrete adjacent sentences until adding the next would cross ``cap``.
    Each input element is already ≤ ``cap``; merged bubbles approach but
    do not cross it."""
    out: list[str] = []
    buf = ""
    for s in sentences:
        if not buf:
            buf = s
            continue
        sep = " " if _needs_space(buf, s) else ""
        if len(buf) + len(sep) + len(s) <= cap:
            buf = buf + sep + s
        else:
            out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return out


def _force_chunks(line: str, hard_max: int) -> list[str]:
    """Word-boundary cut a single oversize sentence into ≤ hard_max pieces.

    Only invoked when one sentence exceeds ``hard_max`` — natural prose
    never reaches this path.
    """
    out: list[str] = []
    rest = line
    while rest:
        if len(rest) <= hard_max:
            out.append(rest)
            break
        cut = _word_boundary_cut(rest, hard_max)
        chunk = rest[:cut]
        out.append(chunk.rstrip() or chunk)
        rest = rest[cut:]
        if rest and rest[0].isspace():
            rest = rest.lstrip()
    return [p for p in out if p]


def _split_long_line(line: str, hard_max: int) -> list[str]:
    """Paragraph-first: a line ≤ ``hard_max`` ships whole. Over the cap we
    sentence-split (bracket-depth aware), force-cut any sentence still over
    ``hard_max``, then accrete sentences back up to ``hard_max`` per
    bubble — long prose ends up as a handful of full-sized bubbles instead
    of a stream of one-clause shards."""
    if len(line) <= hard_max:
        return [line]
    expanded: list[str] = []
    for sentence in _split_into_sentences(line):
        if len(sentence) <= hard_max:
            expanded.append(sentence)
        else:
            expanded.extend(_force_chunks(sentence, hard_max))
    return _merge_sentences(expanded, hard_max)


def split_for_wechat(
    text: str,
    *,
    hard_max: int = DEFAULT_HARD_MAX,
) -> list[str]:
    """Cut assistant text into WeChat-shaped bubbles (text-only).

    Media tags (`<image|file|gif|video path="...">`) are stripped here; use
    `split_for_wechat_typed` to get them back as typed bubbles for dispatch.

    Pipeline:
    1. Strip <!-- buddy: ... --> + media tags + markdown via md_to_plain.
    2. Split on newlines → each non-empty line is a candidate paragraph.
    3. List items (-, ·, *) are emitted whole, never split or merged.
    4. A paragraph ≤ ``hard_max`` ships as one bubble. Over the cap →
       sentence-split (bracket-depth aware) + accrete back to ``hard_max``.
    """
    if not text:
        return []
    text = _MEDIA_TAG.sub("", text)
    flat = md_to_plain(text)
    if not flat:
        return []
    return _text_to_bubbles(flat, hard_max)


def _text_to_bubbles(flat: str, hard_max: int) -> list[str]:
    bubbles: list[str] = []
    for para in re.split(r"\n{2,}", flat):
        para = para.strip()
        if not para:
            continue
        lines = [ln.strip() for ln in para.split("\n") if ln.strip()]
        has_list = any(_is_list_item(ln) for ln in lines)
        if not has_list and len(para) <= hard_max:
            bubbles.append(para)
            continue
        # Has list items or exceeds cap — process line by line
        buf = ""
        for ln in lines:
            if _is_list_item(ln):
                if buf:
                    bubbles.extend(_split_long_line(buf, hard_max))
                    buf = ""
                bubbles.append(ln)
                continue
            if not buf:
                buf = ln
                continue
            candidate = buf + "\n" + ln
            if len(candidate) <= hard_max:
                buf = candidate
            else:
                bubbles.extend(_split_long_line(buf, hard_max))
                buf = ln
        if buf:
            bubbles.extend(_split_long_line(buf, hard_max))
    # Cap text bubbles — merge trailing if over limit
    while len(bubbles) > MAX_WX_BUBBLES:
        last = bubbles.pop()
        bubbles[-1] = bubbles[-1] + "\n" + last
    return [b for b in bubbles if b]


def merge_bubbles_to_cap(
    bubbles: list[dict],
    cap: int,
    *,
    char_ceiling: int = MERGE_CHAR_CEILING,
) -> list[dict]:
    """Merge adjacent TEXT bubbles until ``len(bubbles) <= cap``.

    Text bubbles (`{"kind": "text", ...}`) are joined with ``\\n`` into the
    preceding text bubble. Media bubbles cannot merge — they keep their
    positions and relative order. Merging stops early when no adjacent
    text pair remains, or when the only remaining merge would grow a bubble
    past ``char_ceiling`` (avoids the client's 4000-char re-split, which
    would spawn extra messages and defeat the cap). Pure function: input is
    not mutated.
    """
    out = [dict(b) for b in bubbles]
    while len(out) > cap:
        merged_any = False
        for i in range(len(out) - 1):
            a, b = out[i], out[i + 1]
            if a.get("kind") != "text" or b.get("kind") != "text":
                continue
            combined = f"{a['text']}\n{b['text']}"
            if len(combined) > char_ceiling:
                continue
            a["text"] = combined
            del out[i + 1]
            merged_any = True
            break
        if not merged_any:
            break
    return out


def split_for_wechat_typed(
    text: str,
    *,
    hard_max: int = DEFAULT_HARD_MAX,
) -> list[dict]:
    """Mixed-typed split: text bubbles + media bubbles in source order.

    Text bubbles: `{"kind": "text", "text": "..."}` — same rules as
    `split_for_wechat`.
    Media bubbles: `{"kind": "image"|"file"|"gif"|"video", "path": "..."}`.

    The bridge dispatches text bubbles via `send_text` and media bubbles via
    `send_image / send_file / send_gif / send_video`.
    """
    if not text:
        return []
    out: list[dict] = []
    cursor = 0
    for m in _MEDIA_TAG.finditer(text):
        segment = text[cursor : m.start()]
        if segment.strip():
            flat = md_to_plain(segment)
            for b in _text_to_bubbles(flat, hard_max):
                out.append({"kind": "text", "text": b})
        kind = m.group("kind").lower()
        if kind in _MEDIA_KINDS:
            out.append({"kind": kind, "path": m.group("path")})
        cursor = m.end()
    tail = text[cursor:]
    if tail.strip():
        flat = md_to_plain(tail)
        for b in _text_to_bubbles(flat, hard_max):
            out.append({"kind": "text", "text": b})
    return out
