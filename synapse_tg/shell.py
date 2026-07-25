"""Cortex shell host for the tg bridge: silence cycle, wake ledger, token fuse.

Off unless [cortex].shell_enabled. When on, the bridge hosts a Scheduler
(synapse_core.scheduler) as an internal task and owns one deadline for the tg
shell:

  * next_wake_at from the shell ledger (<state_dir>/tg.json), written by
    marrow's lie_down and announced over the kick socket, when one stands — a
    booked wake suspends the idle cycle instead of racing it; else
  * the silence deadline (idle basis + idle minutes), where the idle basis is
    persisted (last_user_ts) so a restart continues the running window.

An inbound user message cancels the booked wake and restarts the window.

Firing feeds one rendered note turn into the resident session; the reply
streams out to tg like any other turn. Occupancy (same four usage keys cortex
counts) is persisted after every turn; crossing fuse_tokens feeds the fuse
prompt and then respawns a fresh session.

The ledger also carries today's token total the way cortex's cli shell counts
it: tokens_today_base (finished sessions' final occupancies, folded in at every
respawn) + the live occupancy, with tokens_date rolling the base back to 0 on a
new local day.

A directed kick (marrow writes PENDING_NOTE_KEY into the ledger, then kicks)
overrides the deadline: the pending text is claimed atomically and fed as that
round's turn instead of the rendered note. A lie_down(rotate=True) writes
ROTATE_KEY the same way and respawns the resident instead of feeding anything.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from synapse_core import shell_state
from synapse_core.scheduler import Scheduler

if TYPE_CHECKING:
    from .config import TgConfig
    from .loop import TgLoop

logger = logging.getLogger(__name__)

# Context occupancy = the LAST assistant usage's four token keys summed (the
# metric cortex's transcript.window_tokens / watchdog fuse gate use), not a sum
# across turns: cache_read repeats every turn and would multiply.
# Ledger key marrow's directed kick writes: text to feed as the next round.
PENDING_NOTE_KEY = "pending_note"
# Ledger key marrow's lie_down(rotate=True) writes: end this window now.
ROTATE_KEY = "rotate_pending"
# Idle basis: start of the current silence window, persisted so a restart
# continues that window instead of opening a fresh one that fires at once.
LAST_USER_KEY = "last_user_ts"
# Today's token ledger: finished sessions' finals + the local date they belong
# to (cortex reads both to render this shell's "Cortex Today" figure).
TOKENS_BASE_KEY = "tokens_today_base"
TOKENS_DATE_KEY = "tokens_date"

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


def _tzinfo(name: str):
    """Configured display timezone; unknown name -> None (system local)."""
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — a bad tz name must not kill the bridge
        logger.warning("shell: unknown timezone %r — using system local", name)
        return None


class ShellHost:
    """Owns the tg shell's timing, ledger and fuse. One per bridge process."""

    def __init__(self, cfg: "TgConfig", loop: "TgLoop", *, clock=None) -> None:
        self._cfg = cfg
        self._tz = _tzinfo(cfg.timezone)
        self._loop = loop
        self._shell = cfg.shell_id
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        # Same clock on both sides — deadlines computed here are compared to it
        # inside the scheduler loop.
        self._scheduler = Scheduler(socket_path=cfg.shell_socket_path(),
                                    clock=self._clock)
        self._last_user_ts = self._resume_idle_basis()
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

    # --- today ledger ----------------------------------------------------

    def _local_date(self) -> str:
        return datetime.fromtimestamp(self._clock(), self._tz).strftime("%Y-%m-%d")

    def _today_base(self, state: dict, today: str) -> int:
        """Finished sessions' final occupancies for `today`. A ledger stamped
        with any other local date belongs to a past day -> base rolls to 0."""
        if str(state.get(TOKENS_DATE_KEY) or "") != today:
            return 0
        try:
            return max(int(state.get(TOKENS_BASE_KEY) or 0), 0)
        except (TypeError, ValueError):
            return 0

    def fold_session(self, new_sid: str | None = None) -> None:
        """A window ends: its final occupancy joins today's base, so today =
        base + live occupancy stays whole however the window ended (fuse
        respawn, rotate, /clear, /resume, /cwd, cross-channel takeover).

        `new_sid` = the session the incoming provider resumes. Resuming the
        ledger's own session continues that window -> no fold. Idempotent: the
        fold zeroes the ledger's occupancy, so a second call adds 0."""
        state = self._read_state()
        if new_sid and str(state.get("session_id") or "") == str(new_sid):
            return
        today = self._local_date()
        occ = state.get("occupancy")
        occ = occ if isinstance(occ, int) and not isinstance(occ, bool) else 0
        base = self._today_base(state, today) + max(occ, 0)
        self._write_state({"session_id": None, "occupancy": 0,
                           TOKENS_BASE_KEY: base, TOKENS_DATE_KEY: today})

    # --- timing ---------------------------------------------------------

    def _idle_window(self) -> float:
        return self._cfg.shell_idle_min * 60.0

    def _resume_idle_basis(self) -> float:
        """Idle basis at boot: the ledger's, so a restart continues the window
        it was in. Missing, unparseable or already elapsed -> now, i.e. a fresh
        full window — reopening a window never delivers a note by itself."""
        now = self._clock()
        ts = parse_wake_at(self._read_state().get(LAST_USER_KEY))
        return now if ts is None or now - ts >= self._idle_window() else ts

    def _set_idle_basis(self, ts: float, extra: dict | None = None) -> None:
        """Restart the silence window from `ts` in memory and in the ledger."""
        self._last_user_ts = ts
        self._write_state({**(extra or {}), LAST_USER_KEY: _iso(ts)})

    def _silence_deadline(self) -> float:
        return self._last_user_ts + self._idle_window()

    def _deadline(self, state: dict) -> float:
        # A pending direction or rotate is due NOW: this is also what saves a
        # kick that landed while a feed was in flight (the scheduler drops a
        # kick for a shell whose entry is currently firing) — the re-arm after
        # that feed sees the flag and schedules an immediate round.
        if state.get(ROTATE_KEY):
            return self._clock()
        if str(state.get(PENDING_NOTE_KEY) or "").strip():
            return self._clock()
        # A booked wake suspends the idle cycle entirely: while next_wake_at
        # stands it IS the deadline, never raced against the silence one.
        wake = parse_wake_at(state.get("next_wake_at"))
        return self._silence_deadline() if wake is None else wake

    def _arm(self, state: dict | None = None) -> None:
        st = self._read_state() if state is None else state
        at = self._deadline(st)
        self._scheduler.schedule(self._shell, at, self._fire)
        logger.info("shell armed: next round at %s", _iso(at))

    def on_user_message(self) -> None:
        """Any inbound tg message cancels a booked wake and restarts the
        silence cycle from now."""
        self._set_idle_basis(self._clock(), {"next_wake_at": None})
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
        if self._take_rotate():
            # lie_down(rotate=True): the resident has written its handoff and
            # asked to end the window. Same machinery as the fuse respawn minus
            # the fuse prompt; next_wake_at is left standing, so the fresh
            # session sleeps until the wake it just booked.
            logger.info("shell rotate: lie_down(rotate=True) — fresh session")
            await self._loop.shell_rotate()
            self._arm()
            return
        wake = parse_wake_at(state.get("next_wake_at"))
        due = wake is not None and now >= wake
        # Ledger before delivery: the wake is one-shot and the fed round
        # restarts the silence cycle, so a feed that dies mid-flight still
        # costs its window instead of re-firing on the next pass.
        self._set_idle_basis(self._clock(),
                             {"next_wake_at": None} if due else None)
        await self._feed_note(self._take_pending())
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

    def _take_rotate(self) -> bool:
        """Claim marrow's rotate flag (read+clear under one lock), so a rotate
        can neither fire twice nor be dropped by a concurrent write."""
        try:
            raw = shell_state.take(self._cfg.shell_state_dir, self._shell,
                                   ROTATE_KEY)
        except OSError as e:
            logger.warning("shell rotate flag read failed: %s", e)
            return False
        return bool(raw)

    def _take_pending(self) -> str | None:
        """Claim a directed kick's text (read+clear under one lock), so a failed
        feed cannot loop on it and a concurrent write cannot be swallowed."""
        try:
            raw = shell_state.take(self._cfg.shell_state_dir, self._shell,
                                   PENDING_NOTE_KEY)
        except OSError as e:
            logger.warning("shell pending note read failed: %s", e)
            return None
        text = str(raw or "").strip()
        return text or None

    async def _feed_note(self, direction: str | None = None) -> None:
        """One fed round: the directed text when a kick left one, else the
        rendered wakeup note."""
        note = direction or await asyncio.to_thread(self._render_note)
        if not note:
            return  # log already emitted; the cycle re-arms regardless
        logger.info("shell fire: feeding a %s round (%d chars)",
                    "directed" if direction else "note", len(note))
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
        today = self._local_date()
        self._write_state({
            "session_id": self._loop._state.session_id,
            "occupancy": occ,
            TOKENS_BASE_KEY: self._today_base(self._read_state(), today),
            TOKENS_DATE_KEY: today,
        })
        fuse = self._cfg.shell_fuse_tokens
        if self._feeding or fuse <= 0 or occ < fuse:
            return
        logger.info("shell fuse: occupancy=%d >= %d — feeding fuse prompt", occ, fuse)
        await self._feed(f"{self._cfg.shell_fuse_tag}\n{self._cfg.shell_fuse_prompt_text}".strip())
        self._loop.shell_respawn()  # folds today's ledger on the way through


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
