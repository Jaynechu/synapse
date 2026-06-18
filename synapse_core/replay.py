"""B7 — cross-client history replay. synapse-wx/docs/notes/reference.md.

On `/resume <sid>` the bridge tail-reads the cc jsonl session log and emits
the last N turns (user + assistant) with a `[回放]` prefix.
Pure read, no writeback, zero token cost — cc is not invoked.

cc jsonl shape (one event per line; observed in
`~/.claude/projects/<slug>/<sid>.jsonl`):
- `type` ∈ {user, assistant, system, summary, queue-operation, ...}.
- `timestamp` ISO 8601.
- user.message.content: str OR list (tool_result blocks → skip).
- assistant.message.content: list of {type, text|thinking|tool_use,...}.

Slug = cwd with `/` → `-` (cc convention). `marrow_session.py:
jsonl_path_for_sid` walks all slug dirs; we compute one directly because
/resume knows the bridge's cwd, with the same walk as fallback.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPLAY_PREFIX = "[回放]"


def slug_for_cwd(cwd: str) -> str:
    """cc project-dir slug. `/A/B` → `-A-B` (leading `-` preserved)."""
    return cwd.replace("/", "-")


def _parse_ts(raw: Any) -> float:
    """Best-effort ISO 8601 → epoch seconds. 0.0 on failure."""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw:
        return 0.0
    s = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(UTC).timestamp()
    except ValueError:
        return 0.0


def _extract_user_text(message: dict) -> str:
    """Plain user text. tool_result-wrapped events → "" (not human)."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_result":
                # tool_result is cc's machine-generated tool output; not human.
                return ""
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _extract_assistant_text(message: dict) -> str:
    """Concatenated `text` blocks. Drops thinking / tool_use; "" if none."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return "\n".join(parts).strip()


def _jsonl_path(
    sid: str,
    cwd: str | None,
    projects_root: Path | None,
) -> Path | None:
    """Locate sid's jsonl: try cwd-derived slug, else walk projects_root."""
    root = projects_root or (Path.home() / ".claude" / "projects")
    if cwd:
        candidate = Path(root) / slug_for_cwd(cwd) / f"{sid}.jsonl"
        if candidate.is_file():
            return candidate
    if not Path(root).is_dir():
        return None
    for slug_dir in Path(root).iterdir():
        if not slug_dir.is_dir():
            continue
        candidate = slug_dir / f"{sid}.jsonl"
        if candidate.is_file():
            return candidate
    return None


def read_last_n_turns(
    sid: str,
    n: int = 2,
    cwd: str | None = None,
    projects_root: Path | None = None,
) -> list[dict]:
    """Return the last n user + n assistant turns from sid's jsonl.

    Output: chronological list of `{role, content, ts}` dicts, role
    ∈ {"user", "assistant"}. Missing file or empty filter → `[]`.

    Reads the whole file (jsonl is line-streamed). Filters non-user/
    non-assistant events and empty-text turns, then keeps the trailing
    `n` of each role and re-interleaves by timestamp so chronology
    survives.
    """
    if not sid or n <= 0:
        return []
    cwd = cwd if cwd is not None else os.getcwd()
    path = _jsonl_path(sid, cwd, projects_root)
    if path is None:
        return []

    users: list[dict] = []
    assistants: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                etype = ev.get("type")
                if etype not in ("user", "assistant"):
                    continue
                msg = ev.get("message")
                if not isinstance(msg, dict):
                    continue
                if etype == "user":
                    text = _extract_user_text(msg)
                else:
                    text = _extract_assistant_text(msg)
                if not text:
                    continue
                turn = {
                    "role": etype,
                    "content": text,
                    "ts": _parse_ts(ev.get("timestamp")),
                }
                if etype == "user":
                    users.append(turn)
                else:
                    assistants.append(turn)
    except OSError as e:
        logger.warning("replay: jsonl read failed for %s: %s", sid, e)
        return []

    tail_users = users[-n:]
    tail_assistants = assistants[-n:]
    combined = tail_users + tail_assistants
    combined.sort(key=lambda t: t["ts"])
    return combined


def format_for_channel(turns: list[dict]) -> list[str]:
    """Turn replay rows into channel-ready bubbles, one per turn.

    Each bubble is prefixed `[回放] <role>: ` so the user sees the marker
    on every line. Returns `[]` for empty input. The bridge's outbound
    splitter sends one `ILinkClient.send_text` per element.
    """
    out: list[str] = []
    for t in turns:
        role = t.get("role") or "?"
        content = t.get("content") or ""
        out.append(f"{_REPLAY_PREFIX} {role}: {content}")
    return out



def read_turns_since(
    sid: str,
    since_line: int,
    cwd: str | None = None,
    projects_root: Path | None = None,
) -> list[dict]:
    """Return all user/assistant turns after line `since_line` in sid's jsonl.

    Used for incremental replay after cross-channel resume. Falls back to
    read_last_n_turns(n=2) if since_line is beyond current file length.
    """
    if not sid or since_line < 0:
        return []
    cwd = cwd if cwd is not None else os.getcwd()
    path = _jsonl_path(sid, cwd, projects_root)
    if path is None:
        return []

    turns: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_num, raw in enumerate(f):
                if line_num < since_line:
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(ev, dict):
                    continue
                etype = ev.get("type")
                if etype not in ("user", "assistant"):
                    continue
                msg = ev.get("message")
                if not isinstance(msg, dict):
                    continue
                if etype == "user":
                    text = _extract_user_text(msg)
                else:
                    text = _extract_assistant_text(msg)
                if not text:
                    continue
                turns.append({
                    "role": etype,
                    "content": text,
                    "ts": _parse_ts(ev.get("timestamp")),
                })
    except OSError as e:
        logger.warning("replay: jsonl read failed for %s: %s", sid, e)
        return []

    if not turns:
        return read_last_n_turns(sid, n=2, cwd=cwd, projects_root=projects_root)
    return turns
