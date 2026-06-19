"""B9 — atomic tail-truncate of cc's jsonl session file.

`~/.claude/projects/<slug>/<sid>.jsonl` is the source of truth cc replays
from when invoked with `--resume <sid>`. To implement `/rewind N` and
`/regen` the bridge truncates the file in place (tmp + os.replace) and
respawns cc — cc then re-reads the trimmed history.

Public surface
--------------
`drop_last_n_replies(sid, n, cwd, projects_root)`
    Remove the last N assistant reply cycles while keeping the real user
    prompts in place. Tool-result frames are dropped with the assistant
    cycle that produced them. Non-chat lines are kept verbatim.

`extract_user_text(parsed)`
    Pull the plain text of a real user prompt event.

Both helpers tolerate missing files, malformed lines, and N > pairs
without raising. Slug derivation reuses `synapse_core.replay.slug_for_cwd`
so cwd → slug stays in lockstep with the read-side replay logic.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from . import replay

logger = logging.getLogger(__name__)


def _jsonl_path(
    sid: str, cwd: str | None, projects_root: Path | None
) -> Path | None:
    """Locate the jsonl for sid; mirror `replay._jsonl_path` exactly."""
    root = Path(projects_root) if projects_root else (Path.home() / ".claude" / "projects")
    cwd = cwd if cwd is not None else os.getcwd()
    candidate = root / replay.slug_for_cwd(cwd) / f"{sid}.jsonl"
    if candidate.is_file():
        return candidate
    if not root.is_dir():
        return None
    for slug_dir in root.iterdir():
        if not slug_dir.is_dir():
            continue
        c = slug_dir / f"{sid}.jsonl"
        if c.is_file():
            return c
    return None


def _read_lines(path: Path) -> list[tuple[str, dict | None]]:
    """Return ``(raw_line, parsed_or_None)`` per non-blank line.

    Keeping the raw line preserves bytes for non-chat events we re-emit
    verbatim — re-serialising via ``json.dumps`` could subtly reorder keys
    or rewrite whitespace.
    """
    out: list[tuple[str, dict | None]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.rstrip("\n")
                if not stripped.strip():
                    continue
                try:
                    parsed = json.loads(stripped)
                except (ValueError, UnicodeDecodeError):
                    parsed = None
                if not isinstance(parsed, dict):
                    parsed = None
                out.append((stripped, parsed))
    except OSError as e:
        logger.warning("jsonl_edit read failed for %s: %s", path, e)
        return []
    return out


def _atomic_write(path: Path, lines: list[str]) -> None:
    """Write `lines` to `path` via tmp + os.replace. Best-effort on cleanup."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln)
                if not ln.endswith("\n"):
                    f.write("\n")
    except OSError as e:
        logger.warning("jsonl_edit tmp write failed for %s: %s", tmp, e)
        _silent_unlink(tmp)
        return
    try:
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("jsonl_edit replace failed: %s", e)
        _silent_unlink(tmp)


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def is_real_user_prompt(parsed: dict | None) -> bool:
    """True iff parsed is a `type:user` entry whose content is the user's own
    text (not a tool_result block synthesised by cc's tool loop)."""
    if not isinstance(parsed, dict) or parsed.get("type") != "user":
        return False
    msg = parsed.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def drop_last_n_replies(
    sid: str,
    n: int,
    cwd: str | None = None,
    projects_root: Path | None = None,
) -> list[dict]:
    """Drop the last N assistant reply cycles, keeping user prompts.

    A "real user prompt" is a `type:user` entry whose content is the user's
    own text — tool_result entries (also `type:user`) are skipped. The anchor
    is the Nth real user prompt counted from the tail. From that anchor onward,
    assistant lines and tool_result user lines are dropped; real user prompts
    and non-chat lines are kept.

    If fewer than N real prompts exist, considers all of them. Returns dropped
    reply-cycle events in chronological order, or `[]` on missing file, n<=0,
    no real prompts, or no assistant reply to drop.
    """
    if not sid or n <= 0:
        return []
    path = _jsonl_path(sid, cwd, projects_root)
    if path is None:
        return []
    entries = _read_lines(path)
    if not entries:
        return []

    real_indices = [
        i for i, (_, parsed) in enumerate(entries) if is_real_user_prompt(parsed)
    ]
    if not real_indices:
        return []

    take = min(n, len(real_indices))
    cut_idx = real_indices[-take]

    dropped: list[dict] = []
    kept_lines: list[str] = []
    for idx, (raw, parsed) in enumerate(entries):
        if idx < cut_idx or parsed is None:
            kept_lines.append(raw)
            continue
        event_type = parsed.get("type")
        if idx == cut_idx and is_real_user_prompt(parsed):
            kept_lines.append(raw)
        elif event_type in ("user", "assistant"):
            dropped.append(parsed)
        else:
            kept_lines.append(raw)

    if not dropped:
        return []

    _atomic_write(path, kept_lines)
    return dropped


def drop_last_pair(
    sid: str,
    cwd: str | None = None,
    projects_root: Path | None = None,
) -> tuple[list[dict], bool]:
    """Drop the last user+assistant pair for regen. Returns (dropped, has_remaining).

    has_remaining is True if conversation lines survive after the drop
    (multi-turn), False if the jsonl is now conversation-empty (single-turn).
    """
    if not sid:
        return [], False
    path = _jsonl_path(sid, cwd, projects_root)
    if path is None:
        return [], False
    entries = _read_lines(path)
    if not entries:
        return [], False

    real_indices = [
        i for i, (_, parsed) in enumerate(entries) if is_real_user_prompt(parsed)
    ]
    if not real_indices:
        return [], False

    cut_idx = real_indices[-1]

    dropped: list[dict] = []
    kept_lines: list[str] = []
    for idx, (raw, parsed) in enumerate(entries):
        if idx < cut_idx or parsed is None:
            kept_lines.append(raw)
            continue
        if parsed.get("type") in ("user", "assistant"):
            dropped.append(parsed)
        else:
            kept_lines.append(raw)

    has_assistant = any(d.get("type") == "assistant" for d in dropped)
    if not dropped or not has_assistant:
        return [], False

    has_remaining = len(real_indices) > 1

    _atomic_write(path, kept_lines)
    return dropped, has_remaining


def drop_last_n_pairs(
    sid: str,
    n: int,
    cwd: str | None = None,
    projects_root: Path | None = None,
) -> list[dict]:
    """Compatibility wrapper for the old helper name."""
    return drop_last_n_replies(sid, n, cwd=cwd, projects_root=projects_root)


def extract_user_text(parsed: dict | None) -> str | None:
    """Return the user-typed text from a `type:user` jsonl event.

    cc stores prompts as either a string (`content: "hi"`) or a list of
    blocks (`content: [{"type":"text","text":"hi"}, ...]`). Tool_result
    user frames are ignored — they are not Lumi's prose. Returns None
    when the event is not a real user prompt or has no text payload.
    """
    if not is_real_user_prompt(parsed):
        return None
    assert isinstance(parsed, dict)
    msg = parsed.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            txt = block.get("text")
            if isinstance(txt, str) and txt:
                parts.append(txt)
        return "".join(parts) or None
    return None
