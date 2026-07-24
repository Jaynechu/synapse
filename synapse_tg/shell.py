"""Cortex shell host for the tg bridge: silence cycle, wake ledger, token fuse.

Off unless [cortex].shell_enabled. When on, the bridge hosts a Scheduler
(synapse_core.scheduler) as an internal task and owns one deadline for the tg
shell = the nearer of

  * the silence deadline (last user message + idle minutes), and
  * next_wake_at from the shell ledger (<state_dir>/tg.json), written by
    marrow's lie_down and announced over the kick socket.

Firing feeds one rendered note turn into the resident session; the reply
streams out to tg like any other turn. Occupancy (same four usage keys cortex
counts) is persisted after every turn; crossing fuse_tokens feeds the fuse
prompt and then respawns a fresh session.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from synapse_core import shell_state
from synapse_core.scheduler import Scheduler

if TYPE_CHECKING:
    from .config import TgConfig
    from .loop import TgLoop

logger = logging.getLogger(__name__)

# Context occupancy = the LAST assistant usage's four token keys summed (the
# metric cortex's transcript.window_tokens / watchdog fuse gate use), not a sum
# across turns: cache_read repeats every turn and would multiply.
OCCUPANCY_KEYS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def occupancy(usage: dict | None) -> int:
    if not isinstance(usage, dict):
        return 0
    total = 0
    for k in OCCUPANCY_KEYS:
        v = usage.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            total += v
    return total


def parse_wake_at(raw) -> float | None:
    """Ledger ISO timestamp -> wall-clock seconds. A naive value is read as
    local time (marrow writes local ISO). Unparseable -> None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


class ShellHost:
    """Owns the tg shell's timing, ledger and fuse. One per bridge process."""

    def __init__(self, cfg: "TgConfig", loop: "TgLoop", *, clock=None) -> None:
        self._cfg = cfg
        self._loop = loop
        self._shell = cfg.shell_id
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        # Same clock on both sides — deadlines computed here are compared to it
        # inside the scheduler loop.
        self._scheduler = Scheduler(socket_path=cfg.shell_socket_path(),
                                    clock=self._clock)
        self._last_user_ts = self._clock()
        self._feeding = False

    # --- lifecycle ------------------------------------------------------

    async def run(self) -> None:
        self._arm()
        await self._scheduler.run()

    def stop(self) -> None:
        self._scheduler.stop()

    # --- state ----------------------------------------------------------

    def _read_state(self) -> dict:
        try:
            return shell_state.read(self._cfg.shell_state_dir, self._shell)
        except OSError as e:
            logger.warning("shell state read failed: %s", e)
            return {}

    def _write_state(self, data: dict) -> None:
        try:
            shell_state.write(self._cfg.shell_state_dir, self._shell, data)
        except OSError as e:
            logger.warning("shell state write failed: %s", e)

    # --- timing ---------------------------------------------------------

    def _silence_deadline(self) -> float:
        return self._last_user_ts + self._cfg.shell_idle_min * 60.0

    def _deadline(self, state: dict) -> float:
        wake = parse_wake_at(state.get("next_wake_at"))
        silence = self._silence_deadline()
        return silence if wake is None else min(silence, wake)

    def _arm(self, state: dict | None = None) -> None:
        st = self._read_state() if state is None else state
        self._scheduler.schedule(self._shell, self._deadline(st), self._fire)

    def on_user_message(self) -> None:
        """Any inbound tg message restarts the silence cycle."""
        self._last_user_ts = self._clock()
        self._arm()

    # --- fire -----------------------------------------------------------

    async def _fire(self, shell: str) -> None:
        """Scheduler callback. Also the kick landing point, so it re-reads the
        ledger first: a kick that only announced a FUTURE next_wake_at just
        re-arms, no note."""
        state = self._read_state()
        now = self._clock()
        if now < self._deadline(state):
            self._arm(state)
            return
        wake = parse_wake_at(state.get("next_wake_at"))
        if wake is not None and now >= wake:
            self._write_state({"next_wake_at": None})  # one-shot ledger entry
        await self._feed_note()
        self._last_user_ts = self._clock()  # a fed round re-arms the cycle
        self._arm()

    def _render_note(self) -> str | None:
        cmd = self._cfg.shell_note_render_cmd
        if not cmd:
            logger.warning("shell note render: [cortex].note_render_cmd unset — round skipped")
            return None
        try:
            proc = subprocess.run(
                list(cmd), capture_output=True, text=True,
                timeout=self._cfg.shell_note_render_timeout_s,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("shell note render failed: %s", e)
            return None
        if proc.returncode != 0:
            logger.warning("shell note render exited %d: %s",
                           proc.returncode, (proc.stderr or "")[:200])
            return None
        return (proc.stdout or "").strip() or None

    async def _feed_note(self) -> None:
        note = await asyncio.to_thread(self._render_note)
        if not note:
            return  # log already emitted; the cycle re-arms regardless
        body = f"{self._cfg.shell_note_tag}\n{note}".strip()
        if await self._feed(body):
            self._write_state({"last_note_ts": _iso(self._clock())})
            await self.after_turn()  # a fed round burns tokens too

    async def _feed(self, body: str) -> bool:
        self._feeding = True
        try:
            return await self._loop.feed_turn(body)
        finally:
            self._feeding = False

    # --- token ledger + fuse --------------------------------------------

    async def after_turn(self) -> None:
        """Called by the loop once a resident turn has been delivered."""
        occ = occupancy(self._loop._state.last_assistant_usage)
        self._write_state({
            "session_id": self._loop._state.session_id,
            "occupancy": occ,
        })
        fuse = self._cfg.shell_fuse_tokens
        if self._feeding or fuse <= 0 or occ < fuse:
            return
        logger.info("shell fuse: occupancy=%d >= %d — feeding fuse prompt", occ, fuse)
        await self._feed(f"{self._cfg.shell_fuse_tag}\n{self._cfg.shell_fuse_prompt_text}".strip())
        self._loop.shell_respawn()
        self._write_state({"session_id": None, "occupancy": 0})


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
