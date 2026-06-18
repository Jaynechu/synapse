"""Persist a tiny slice of BridgeState across bridge crashes.

Only `effort_level`, `thinking_on`, `quote_on` are persisted — everything else
is session-scoped and a crash should reset it. `model` is intentionally NOT
persisted: bridge starts on the caller's configured default model, ``/resume
<sid>`` pulls the historic model from marrow.sessions, and ``/swap`` sets it
for the current session only. The caller owns the storage path.

The file write uses :func:`marrow._atomic.atomic_write` when marrow is
importable; otherwise it falls back to a tempfile + ``os.replace`` pattern so
the bridge keeps working in isolated tests.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

PERSISTED_KEYS: tuple[str, ...] = (
    "effort_level",
    "thinking_on",
    "quote_on",
    "voice_style",
    "cc_cwd",
    "session_id",
)


def _atomic_write_fallback(path: Path, data: str) -> None:
    path = Path(os.path.realpath(str(path)))
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".swx.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _atomic_write(path: Path, data: str) -> None:
    try:
        from marrow._atomic import atomic_write
    except Exception:
        _atomic_write_fallback(path, data)
        return
    atomic_write(str(path), data)


def load(path: Path) -> dict:
    """Read the persisted state file. Missing/malformed → empty dict."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        logger.warning("bridge_state load failed (%s); ignoring", e)
        return {}
    if not isinstance(data, dict):
        return {}
    # Only keep persisted keys — drop anything else silently.
    return {k: data[k] for k in PERSISTED_KEYS if k in data}


def save(path: Path, data: dict) -> None:
    """Write the persisted-keys subset of `data` atomically."""
    p = Path(path)
    payload = {k: data[k] for k in PERSISTED_KEYS if k in data}
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    try:
        _atomic_write(p, body)
    except OSError as e:
        logger.warning("bridge_state save failed (%s); state will reset on crash", e)
