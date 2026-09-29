"""Local wall-clock time for every synapse channel.

One source: `[core].timezone`. An empty or unknown name means the OS timezone.
A bridge calls `configure(cfg.timezone)` once at boot; every helper takes an
optional tzinfo and otherwise falls back to that process-wide zone.
"""

from __future__ import annotations

import logging
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_configured: tzinfo | None = None


def resolve_tz(name: str | None) -> tzinfo | None:
    """IANA name -> tzinfo; empty/unknown name -> None (OS timezone)."""
    if not (name or "").strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except Exception:  # noqa: BLE001 — a bad tz name must not kill the bridge
        logger.warning("timeutil: unknown timezone %r — using system local", name)
        return None


def configure(name: str | None) -> None:
    """Set the process-wide zone from `[core].timezone`."""
    global _configured
    _configured = resolve_tz(name)


def local_tz() -> tzinfo | None:
    """The process-wide zone; None means the OS timezone."""
    return _configured


def _zone(tz: tzinfo | None) -> tzinfo | None:
    return tz if tz is not None else _configured


def local_now(tz: tzinfo | None = None) -> datetime:
    """Aware now in `tz` (default: the configured zone)."""
    zone = _zone(tz)
    return datetime.now(zone) if zone is not None else datetime.now().astimezone()


def from_timestamp(ts: float, tz: tzinfo | None = None) -> datetime:
    """Epoch seconds -> aware local datetime."""
    zone = _zone(tz)
    if zone is not None:
        return datetime.fromtimestamp(ts, zone)
    return datetime.fromtimestamp(ts).astimezone()


def to_local(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Aware -> converted to local; naive -> read as local wall time."""
    zone = _zone(tz)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zone) if zone is not None else dt.astimezone()
    return dt.astimezone(zone)
