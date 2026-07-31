"""Circuit breaker ("main switch") — bridge side.

ONE persistent file decides whether a cortex shell may run AUTONOMOUS activity
(fed rounds, respawns). Bridges keep running and normal chat is untouched.

    <marrow config dir>/breaker.json
        {"scope": "all"|"cli"|"tg", "reason": "auto_fuse"|"manual", "ts": iso}
    file absent  = breaker clear
    unreadable   = treated as clear (logged), never wedges the bridge

Sibling tally, appended by BOTH shells:

    <marrow config dir>/fuse_events.json
        {"events": [{"ts": iso, "shell": "cli"|"tg"}, ...]}
    pruned to [cortex.breaker].window_hours on every write

Sibling duty hold, owned by the cortex-side duty rotation (this repo never
writes it, covers() only unions it in):

    <marrow config dir>/duty.json
        {"mode": ..., "hold": "cli"|"tg"|"all"|null, "ts": iso}
    absent/corrupt = no duty hold (fail-open)

The JSON file IS the cross-repo protocol. Protocol copied, never imported (same
rule as synapse_core.shell_state): the cortex repo ships its own independent
copy (cortex/breaker.py). Thresholds live ONLY in marrow's config.toml under
[cortex.breaker], read directly here so the same number is never duplicated
into the tg bridge config.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

BREAKER_FILE = "breaker.json"
FUSE_FILE = "fuse_events.json"
DUTY_FILE = "duty.json"

SCOPE_ALL = "all"
REASON_AUTO = "auto_fuse"
REASON_MANUAL = "manual"

DEFAULTS = {
    "enabled": True,
    "fuse_threshold": 2,
    "window_hours": 24,
    "trip_message": (
        "Circuit breaker tripped: fuse #{count} within {hours}h. Cortex "
        "autonomous activity paused ({scope}). Clear with ct-duty cli|tg|all."
    ),
}


# --- paths ---------------------------------------------------------------

def breaker_path(config_dir: str | os.PathLike[str]) -> Path:
    return Path(config_dir).expanduser() / BREAKER_FILE


def fuse_path(config_dir: str | os.PathLike[str]) -> Path:
    return Path(config_dir).expanduser() / FUSE_FILE


def duty_path(config_dir: str | os.PathLike[str]) -> Path:
    return breaker_path(config_dir).with_name(DUTY_FILE)


def settings(config_dir: str | os.PathLike[str]) -> dict:
    """[cortex.breaker] from marrow's config.toml, layered over DEFAULTS.
    Missing file / section / key -> defaults. Never raises."""
    out = dict(DEFAULTS)
    p = Path(config_dir).expanduser() / "config.toml"
    try:
        if p.is_file():
            data = tomllib.loads(p.read_bytes().decode("utf-8"))
            section = (data.get("cortex") or {}).get("breaker") or {}
            if isinstance(section, dict):
                for k in DEFAULTS:
                    if k in section:
                        out[k] = section[k]
    except (OSError, UnicodeDecodeError, ValueError, TypeError) as e:
        logger.warning("breaker settings read failed (%s) — using defaults", e)
    return out


# --- io ------------------------------------------------------------------

@contextlib.contextmanager
def _lock(p: Path):
    """Advisory lock on a `.lock` sibling around read-modify-write. Best effort:
    a lock we cannot take must never wedge the bridge."""
    lp = p.with_suffix(".lock")
    fd = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError as e:
        logger.warning("breaker lock failed (%s) — proceeding unlocked", e)
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _read_json(p: Path, default):
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, type(default)):
                return data
            logger.warning("breaker file %s has unexpected shape — ignoring", p.name)
    except (OSError, ValueError) as e:
        logger.warning("breaker file %s unreadable (%s) — treated as clear", p.name, e)
    return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# --- state ---------------------------------------------------------------

def read(config_dir: str | os.PathLike[str]) -> dict | None:
    """Current breaker state, or None when clear. A corrupt/odd file reads as
    clear (logged) — a broken breaker must never silently freeze the shell."""
    d = _read_json(breaker_path(config_dir), {})
    scope = str(d.get("scope") or "").strip().lower()
    if not scope:
        return None
    return {"scope": scope,
            "reason": str(d.get("reason") or REASON_MANUAL),
            "ts": str(d.get("ts") or "")}


def duty_hold(config_dir: str | os.PathLike[str]) -> str | None:
    """The materialised duty hold, or None. Read-only: this repo never writes
    duty.json — it is owned by the cortex-side duty rotation."""
    d = _read_json(duty_path(config_dir), {})
    hold = d.get("hold")
    if not isinstance(hold, str) or not hold.strip():
        return None
    return hold.strip().lower()


def covers(config_dir: str | os.PathLike[str], shell: str) -> bool:
    """Does the breaker OR the duty hold pin `shell` down right now?"""
    shell = str(shell).strip().lower()
    state = read(config_dir)
    if state is not None and state["scope"] in (SCOPE_ALL, shell):
        return True
    hold = duty_hold(config_dir)
    return hold in (SCOPE_ALL, shell)


def trip(config_dir: str | os.PathLike[str], scope: str = SCOPE_ALL,
         reason: str = REASON_MANUAL, *, now: datetime | None = None) -> dict:
    """Write the breaker. Last writer wins — an auto trip and a manual pause
    both mean 'stop', so there is nothing to merge."""
    state = {"scope": str(scope).strip().lower() or SCOPE_ALL,
             "reason": reason,
             "ts": (now or datetime.now().astimezone()).isoformat()}
    p = breaker_path(config_dir)
    with _lock(p):
        _write_json(p, state)
    return state


def clear(config_dir: str | os.PathLike[str]) -> bool:
    """Remove the breaker entirely. True when one was standing."""
    p = breaker_path(config_dir)
    with _lock(p):
        had = read(config_dir) is not None
        with contextlib.suppress(OSError):
            p.unlink(missing_ok=True)
    return had


# --- fuse tally ----------------------------------------------------------

def _parse(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def _prune(events: list, cutoff: datetime) -> list:
    kept = []
    for e in events:
        if isinstance(e, str):
            e = {"ts": e, "shell": ""}
        if not isinstance(e, dict):
            continue
        dt = _parse(e.get("ts"))
        if dt is not None and dt >= cutoff:
            kept.append({"ts": dt.isoformat(), "shell": str(e.get("shell") or "")})
    return kept


def record_fuse(config_dir: str | os.PathLike[str], shell: str, *,
                now: datetime | None = None) -> int:
    """Append one fuse event for `shell`, prune the rolling window, return the
    surviving count ACROSS ALL SHELLS (both write this same file)."""
    cfg = settings(config_dir)
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(hours=float(cfg["window_hours"]))
    p = fuse_path(config_dir)
    with _lock(p):
        data = _read_json(p, {})
        events = data.get("events")
        events = _prune(events if isinstance(events, list) else [], cutoff)
        events.append({"ts": now.isoformat(), "shell": str(shell)})
        _write_json(p, {"events": events})
    return len(events)


def record_fuse_and_maybe_trip(config_dir: str | os.PathLike[str], shell: str, *,
                               now: datetime | None = None) -> tuple[int, dict | None]:
    """Record a fuse, then trip the breaker (scope=all, reason=auto_fuse) when
    the rolling count reaches the threshold and [cortex.breaker].enabled.

    Returns (count, tripped_state_or_None). `enabled = false` still tallies —
    the count is the useful signal — it just never trips."""
    cfg = settings(config_dir)
    count = record_fuse(config_dir, shell, now=now)
    if not bool(cfg["enabled"]):
        return count, None
    try:
        threshold = int(cfg["fuse_threshold"])
    except (TypeError, ValueError):
        threshold = int(DEFAULTS["fuse_threshold"])
    if threshold <= 0 or count < threshold:
        return count, None
    return count, trip(config_dir, SCOPE_ALL, REASON_AUTO, now=now)


def trip_message(config_dir: str | os.PathLike[str], count: int,
                 scope: str = SCOPE_ALL) -> str:
    cfg = settings(config_dir)
    try:
        return str(cfg["trip_message"]).format(
            count=count, hours=cfg["window_hours"], scope=scope)
    except (KeyError, IndexError, ValueError):
        return str(cfg["trip_message"])
