"""Raw getupdates payload logger — typing-event hunt (PLAN batch item 2c).

Dumps every interesting poll response to a jsonl file so we can audit
whether iLink ever surfaces an inbound typing event. Date-gated: set
`[debug] raw_poll_log_until = "YYYY-MM-DD"` in config.toml; logging stops
automatically after that local date, so a forgotten flag can't grow the
file forever.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from synapse_core.timeutil import local_now

logger = logging.getLogger(__name__)

DEFAULT_RAW_LOG_PATH = (
    Path.home() / "Library" / "Logs" / "synapse-wx-poll-raw.jsonl"
)

# Keys every getupdates response carries; anything else is a surprise worth
# logging even when `msgs` is empty (e.g. a typing/presence side-channel).
_BORING_KEYS = frozenset({"ret", "errmsg", "get_updates_buf", "msgs"})


class RawPollLogger:
    """Append-only jsonl sink for raw poll payloads, active until a date."""

    def __init__(
        self,
        until: str,
        path: Path = DEFAULT_RAW_LOG_PATH,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._until = until
        self._path = Path(path)
        self._now = now or local_now

    def active(self) -> bool:
        if not self._until:
            return False
        return self._now().date().isoformat() <= self._until

    @staticmethod
    def _interesting(data: dict) -> bool:
        if data.get("msgs"):
            return True
        return any(k not in _BORING_KEYS for k in data)

    def log(self, data: dict) -> None:
        """Best-effort append; never raises into the poll path."""
        try:
            if not isinstance(data, dict):
                return
            if not self.active() or not self._interesting(data):
                return
            line = json.dumps(
                {"ts": self._now().isoformat(), "data": data},
                ensure_ascii=False,
                default=str,
            )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.warning("raw poll log write failed: %s", e)
