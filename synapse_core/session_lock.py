"""Cross-channel session lock.

When a bridge /resume's a session, it claims the sid so other channels
know not to keep writing to the same jsonl.  Storage is a tiny JSON
file at ~/.config/marrow/session_claims.json — both bridges read it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("~/.config/marrow/session_claims.json").expanduser()


def _read(path: Path = _DEFAULT_PATH) -> dict[str, str]:
    try:
        return json.loads(path.read_text("utf-8")) if path.exists() else {}
    except Exception as e:
        logger.warning("session_lock read failed: %s", e)
        return {}


def _write(data: dict[str, str], path: Path = _DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    d = path.parent
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".slock.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, str(path))
    except Exception as e:
        logger.warning("session_lock write failed: %s", e)
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def claim(sid: str, channel: str, path: Path = _DEFAULT_PATH) -> None:
    if not sid:
        return
    data = _read(path)
    data[sid] = channel
    _write(data, path)
    logger.debug("session_lock claim %s → %s", sid[:8], channel)


def release(sid: str, channel: str, path: Path = _DEFAULT_PATH) -> None:
    if not sid:
        return
    data = _read(path)
    if data.get(sid) == channel:
        del data[sid]
        _write(data, path)
        logger.debug("session_lock release %s (%s)", sid[:8], channel)


def holder(sid: str, path: Path = _DEFAULT_PATH) -> str | None:
    if not sid:
        return None
    return _read(path).get(sid)
