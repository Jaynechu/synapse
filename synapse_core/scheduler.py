"""Per-shell timing engine: sleep-until-nearest, fire callback, recompute.

Pure timing — owns no business logic. Host process injects callbacks via
:meth:`Scheduler.schedule`; the module only tracks ``at`` deadlines per shell,
sleeps until the nearest, fires, and recomputes. An external kick over a unix
domain socket (stdlib only) interrupts the current sleep instantly; a safety
recompute tick (default 60s) is a backstop against lost kicks / clock drift.

Clock and sleep are injectable so tests drive the loop with a mocked clock and
never sleep for real.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import time as _wall_time

logger = logging.getLogger(__name__)

Callback = Callable[[str], Awaitable[None] | None]
SafetyTick = 60.0


class Scheduler:
    """Single asyncio loop scheduling one deadline per shell.

    schedule(shell, at, callback) — set/replace a shell's deadline (wall-clock
    ``at``, seconds) and callback. cancel(shell) — drop it. kick(shell) — fire
    a shell now, whatever its deadline. All three interrupt the current sleep.

    Deadlines are one-shot: firing consumes the entry. A callback that wants a
    repeating cycle re-schedules itself.
    """

    def __init__(
        self,
        *,
        socket_path: str | os.PathLike[str] | None = None,
        safety_tick: float = SafetyTick,
        clock: Callable[[], float] = _wall_time,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._table: dict[str, tuple[float, Callback]] = {}
        self._pending_kicks: set[str] = set()
        self._safety_tick = safety_tick
        self._clock = clock
        self._sleep = sleep or self._default_sleep
        self._wake = asyncio.Event()
        self._socket_path = Path(socket_path) if socket_path else None
        self._server: asyncio.AbstractServer | None = None
        self._stopped = False

    # --- public API -----------------------------------------------------

    def schedule(self, shell: str, at: float, callback: Callback) -> None:
        """Set/replace ``shell``'s deadline (wall-clock ``at``) and callback."""
        self._table[shell] = (at, callback)
        self._wake.set()

    def cancel(self, shell: str) -> None:
        """Drop ``shell``'s deadline; no-op if absent."""
        self._table.pop(shell, None)
        self._pending_kicks.discard(shell)
        self._wake.set()

    def kick(self, shell: str) -> None:
        """Fire ``shell`` on the next loop pass, ignoring its deadline."""
        if shell in self._table:
            self._pending_kicks.add(shell)
            self._wake.set()

    # --- loop -----------------------------------------------------------

    async def run(self) -> None:
        """Run the loop until :meth:`stop`. Opens the kick socket if configured."""
        await self._open_socket()
        try:
            while not self._stopped:
                await self._tick()
        finally:
            await self._close_socket()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def _tick(self) -> None:
        now = self._clock()
        due = self._pending_kicks | {s for s, (at, _) in self._table.items() if at <= now}
        if due:
            self._pending_kicks -= due
            for shell in due:
                await self._fire(shell)
            return
        timeout = self._time_to_nearest(now)
        self._wake.clear()
        await self._sleep(timeout)

    def _time_to_nearest(self, now: float) -> float:
        if not self._table:
            return self._safety_tick
        nearest = min(at for at, _ in self._table.values())
        return max(0.0, min(self._safety_tick, nearest - now))

    async def _fire(self, shell: str) -> None:
        entry = self._table.pop(shell, None)  # one-shot: consume the deadline
        if entry is None:
            return
        _, callback = entry
        try:
            result = callback(shell)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 — host callback, never kill the loop
            logger.exception("scheduler callback failed for shell %s", shell)

    async def _default_sleep(self, timeout: float) -> None:
        """Wait for a kick/schedule/cancel or the timeout, whichever first."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except (TimeoutError, asyncio.TimeoutError):
            pass

    # --- kick socket ----------------------------------------------------

    async def _open_socket(self) -> None:
        if self._socket_path is None:
            return
        p = self._socket_path
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        self._server = await asyncio.start_unix_server(self._on_conn, path=str(p))

    async def _close_socket(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            finally:
                self._server = None
        if self._socket_path is not None and self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass

    async def _on_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """One line per connection = a shell id to kick."""
        try:
            data = await reader.readline()
            shell = data.decode("utf-8", "replace").strip()
            if shell:
                self.kick(shell)
        finally:
            writer.close()


async def send_kick(socket_path: str | os.PathLike[str], shell: str) -> None:
    """Client helper: connect to a running scheduler's socket and kick ``shell``."""
    reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
    try:
        writer.write((shell + "\n").encode("utf-8"))
        await writer.drain()
    finally:
        writer.close()
