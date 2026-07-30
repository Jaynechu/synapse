"""Per-shell cortex state file (<state_dir>/<shell>.json).

Synapse-side accessor for the ledger a cortex shell host and marrow share.
Protocol copied (never imported — the two repos stay independent): flock on a
`<shell>.lock` sibling, read-modify-write, atomic replace. Unknown keys written
by the other side survive a merge here, and vice versa.

Keys: session_id / next_wake_at / last_note_ts / last_user_ts / occupancy /
pending_note / rotate_pending / tokens_today_base / tokens_date.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.02


def state_path(state_dir: str | os.PathLike[str], shell: str) -> Path:
    return Path(state_dir).expanduser() / f"{shell}.json"


@contextlib.contextmanager
def _lock(p: Path):
    """flock the `.lock` sibling; proceed anyway after the timeout (matches the
    marrow/cortex fallback — a stuck lock must never wedge the bridge)."""
    lp = p.with_suffix(".lock")
    fd = None
    got = False
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(_LOCK_POLL_S)
        yield
    finally:
        if fd is not None:
            if got:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _load(p: Path) -> dict:
    try:
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def _save(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def read(state_dir: str | os.PathLike[str], shell: str) -> dict:
    """Whole state dict under the lock. Missing/corrupt file -> {}."""
    p = state_path(state_dir, shell)
    with _lock(p):
        return _load(p)


def take(state_dir: str | os.PathLike[str], shell: str, key: str):
    """Read and clear `key` inside ONE critical section, so a value claimed here
    can never be fed twice nor dropped by a concurrent write. Absent -> None."""
    p = state_path(state_dir, shell)
    with _lock(p):
        d = _load(p)
        if key not in d:
            return None
        value = d.pop(key)
        _save(p, d)
        return value


def write(state_dir: str | os.PathLike[str], shell: str, data: dict) -> Path:
    """Merge `data` into the shell's state file under the lock; a None value
    drops its key. Keys this side does not know are left untouched."""
    p = state_path(state_dir, shell)
    with _lock(p):
        d = _load(p)
        for k, v in data.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        _save(p, d)
    return p
