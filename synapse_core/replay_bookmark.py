"""Replay bookmark — track where each channel last read a session's jsonl.

When a session leaves a channel (clear, resume-away, claimed-away), save
the current jsonl line count. On resume, replay only turns after that line.

Storage: ~/.config/marrow/replay_bookmarks.json
Format: {sid: {channel: line_count}}
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from synapse_core.replay import _jsonl_path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("~/.config/marrow/replay_bookmarks.json").expanduser()


def _read(path: Path = _DEFAULT_PATH) -> dict:
    try:
        return json.loads(path.read_text("utf-8")) if path.exists() else {}
    except Exception as e:
        logger.warning("replay_bookmark read failed: %s", e)
        return {}


def _write(data: dict, path: Path = _DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rbm.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, str(path))
    except Exception as e:
        logger.warning("replay_bookmark write failed: %s", e)
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _count_lines(sid: str, cwd: str | None) -> int | None:
    """Count lines in sid's jsonl. Returns None if file not found."""
    path = _jsonl_path(sid, cwd, None)
    if path is None:
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def save(sid: str, channel: str, cwd: str | None = None, path: Path = _DEFAULT_PATH) -> None:
    """Save current jsonl line count as bookmark for this channel."""
    if not sid or not channel:
        return
    count = _count_lines(sid, cwd)
    if count is None:
        return
    data = _read(path)
    data.setdefault(sid, {})[channel] = count
    _write(data, path)
    logger.debug("replay_bookmark save %s/%s = %d", sid[:8], channel, count)


def load(sid: str, channel: str, path: Path = _DEFAULT_PATH) -> int | None:
    """Load bookmark line count. Returns None if no bookmark."""
    if not sid or not channel:
        return None
    data = _read(path)
    entry = data.get(sid)
    if not isinstance(entry, dict):
        return None
    val = entry.get(channel)
    return int(val) if val is not None else None


def clear(sid: str, channel: str | None = None, path: Path = _DEFAULT_PATH) -> None:
    """Remove bookmark(s) for a sid. If channel given, only that one."""
    if not sid:
        return
    data = _read(path)
    if sid not in data:
        return
    if channel:
        data[sid].pop(channel, None)
        if not data[sid]:
            del data[sid]
    else:
        del data[sid]
    _write(data, path)
