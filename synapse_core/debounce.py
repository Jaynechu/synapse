"""Inbound message buffer: accumulate WeChat bubbles, flush after quiet window."""

from __future__ import annotations

import re
import time
from collections.abc import Callable

# Trailing hold marker: bubble ends with a "not done yet" signal — 3+ ascii
# dots, 3+ CJK full stops, 1+ U+2026 (IME "……" is two), or full-width ～
# (U+FF5E, one-tap on the CN mobile keyboard). Half-width ~ is
# deliberately excluded.
_TRAILING_HOLD = re.compile(r"(?:\.{3,}|。{3,}|…+|～)$")


class InboundBuffer:
    """Accumulate consecutive WeChat bubbles from one user, flush after quiet window.

    HOLD_WORDS are exact strip-equality matches against a single bubble — substring
    matches (e.g. "等下") deliberately do NOT extend the window. A trailing
    marker (…… / ... / 。。。 / ～) on any bubble also extends it.
    Hold-window upgrade is sticky: once extended, a later normal bubble keeps
    the longer window.
    """

    HOLD_WORDS: tuple[str, ...] = ("等", "稍等", "等等", "先")
    DEFAULT_QUIET_SEC: float = 5.0
    HOLD_QUIET_SEC: float = 10.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._bubbles: list[str] = []
        self._first_ts: float = 0.0
        self._last_ts: float = 0.0
        self._quiet_target: float = self.DEFAULT_QUIET_SEC
        self._clock = clock

    def add(self, text: str) -> None:
        """Append one bubble; extend quiet window if hold word seen.

        Empty / whitespace-only bubbles are dropped silently.
        """
        if text is None:
            return
        stripped = text.strip()
        if not stripped:
            return
        now = self._clock()
        if not self._bubbles:
            self._first_ts = now
            self._quiet_target = self.DEFAULT_QUIET_SEC
        self._bubbles.append(stripped)
        self._last_ts = now
        if stripped in self.HOLD_WORDS or _TRAILING_HOLD.search(stripped):
            self._quiet_target = self.HOLD_QUIET_SEC

    def prepend(self, text: str) -> None:
        """Re-insert a previously flushed body at the FRONT of the buffer.

        Used by the pre-send merge path: a reply was dropped because new
        bubbles arrived mid-turn, so the old body must precede them in the
        merged prompt. Does NOT touch timestamps — the quiet window keeps
        counting from the newest live bubble.
        """
        if not text or not text.strip():
            return
        self._bubbles.insert(0, text)

    def ready(self) -> bool:
        """True if buffer non-empty AND quiet window elapsed since last bubble."""
        if not self._bubbles:
            return False
        return (self._clock() - self._last_ts) >= self._quiet_target

    def flush(self) -> str:
        """Return joined bubbles ('\\n' separator) and clear buffer state."""
        joined = "\n".join(self._bubbles)
        self._bubbles = []
        self._first_ts = 0.0
        self._last_ts = 0.0
        self._quiet_target = self.DEFAULT_QUIET_SEC
        return joined

    def __len__(self) -> int:
        return len(self._bubbles)

    def __bool__(self) -> bool:
        return bool(self._bubbles)
