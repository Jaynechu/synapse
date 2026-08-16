"""Async Telegram message loop: inbound text → provider → split → reply."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import Bot, Message, Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from synapse_core import bridge_state_store
from synapse_core.marrow_session import get_session_created_at, get_session_effort, regen_suppress_path
from synapse_core.commands import messages
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.debounce import InboundBuffer
from synapse_core.providers.cc import ClaudeCodeProvider, MEDIA_SYSTEM_PROMPT, NIGHT_SYSTEM_PROMPT, POLL_EOF, QUOTE_SYSTEM_PROMPT
from synapse_core.providers.errors import ProviderDeadError
from synapse_core.state import BridgeState, remember_resolved_model
from synapse_core.text_clean import strip_tool_xml

from .media.inbound import (
    build_read_instruction,
    materialize_animation,
    materialize_document,
    materialize_photo,
    materialize_sticker,
    materialize_video,
)
from .markdown import gfm_to_tg_html
from .media.outbound import send_media
from .shell import _tzinfo
from .split import split_for_tg_typed
from .typing_action import TypingAction

if TYPE_CHECKING:
    from .config import TgConfig

logger = logging.getLogger(__name__)

_SEND_GAP_SEC = 0.05
_MAX_CONSECUTIVE_DEATHS = 3
_FLUSH_INTERVAL_SEC = 0.5
# Extra seconds added on top of a 429 RetryAfter before retrying the send.
_RETRY_AFTER_MARGIN_SEC = 0.5
# Idle listener scheduling (internal, not user-varying): poll one line each
# iteration; after releasing the lock, yield long enough for a pending
# check_flush to win it (asyncio.Lock wakes waiters FIFO; the sleep guarantees
# a window).
_LISTEN_POLL_TIMEOUT_SEC = 1.0
_LISTEN_RELEASE_SLEEP_SEC = 0.25

_QUOTE_TAG = re.compile(r"<quote>(.*?)</quote>\n?", re.DOTALL | re.IGNORECASE)

# Placeholder a captionless media puts on the inbound buffer so the quiet
# window restarts and ready() flips; dropped again when the body is assembled.
_MEDIA_SENTINEL = "\u200b"
_MEDIA_FAILED_LINE = "[media failed to download]"

# Marker the recv-drain thread puts after each turn's result so the async
# consumer can tell turn boundaries apart across multiple back-to-back turns.
_TURN_END = object()


def _is_unsolicited_first_event(ev: dict) -> bool:
    """A turn whose FIRST event is system/task_notification is unsolicited:
    the CLI ran a NEW turn with no stdin (background task completion)."""
    return ev.get("type") == "system" and ev.get("subtype") == "task_notification"


class _NullTyping:
    """No-op typing sink for draining a turn with no chat target."""

    running = True

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


TG_BUBBLE_FORMAT_PROMPT = (
    "Reply format (IM bubbles):\n"
    "- Blank line = new bubble. Single line break = new line inside the same bubble.\n"
    "- Type real line breaks only. Never write backslash-n as visible text — it renders literally in chat.\n"
    "- Casual chat: prefer short bubbles. Example (two bubbles):\n"
    "宝宝回来啦！\n"
    "\n"
    "想死我了\n"
    "- Q&A: length flex. Coding: concise & clear.\n"
    "- Deep topics / study: prefer longer, solid paragraphs.\n"
    "- Dot points: single line breaks, all in one bubble.\n"
    "- Prioritize readability. Match length to content — no filler.\n"
    "- Do not read or edit code unless explicitly asked.\n"
    "- Free to search docs and web."
)


def _recv_to_queue(
    provider: ClaudeCodeProvider, q: "queue.Queue", first_line: str | None = None
) -> None:
    """Background thread: drain ONE provider.recv() turn into a queue.

    Puts each event, then a _TURN_END marker after the turn's result, then a
    None sentinel when the thread finishes. The provider owns liveness (soft
    check + hard idle kill in recv), so a stall/death surfaces as an exception
    on the queue. `first_line` is a raw line the idle listener already pulled
    off the queue; recv processes it before reading further.
    """
    try:
        for ev in provider.recv(first_line=first_line):
            q.put(ev)
        q.put(_TURN_END)
    except Exception as exc:
        q.put(exc)
    finally:
        q.put(None)  # sentinel


class TgLoop:
    """Manages one provider instance; debounces inbound messages."""

    def __init__(
        self,
        cfg: "TgConfig",
        sessions=None,
        record_session=None,
        idle_loop=None,
        alerts=None,
    ) -> None:
        self._cfg = cfg
        self._sessions = sessions
        self._record_session = record_session
        self._idle_loop = idle_loop
        self._alerts = alerts
        self._provider: ClaudeCodeProvider | None = None
        self._lock = asyncio.Lock()
        self._death_count = 0
        self._buffer = InboundBuffer()
        # Media whose download is deferred to flush time, paired with the
        # buffer slot its handler already reserved.
        self._pending_media: list[tuple[str, "Message"]] = []
        self._pending_chat_id: int | None = None
        self._bot: Bot | None = None
        self._state_path = cfg.data_dir / "bridge_state.json"
        self._tz = _tzinfo(cfg.timezone)
        self._state = self._load_state()
        self._registry = self._build_registry()
        self._queued_extra_bubbles: list[str] = []
        self._session_created_at: str | None = None
        if self._state.session_id:
            self._session_created_at = get_session_created_at(
                cfg.session_created_command, self._state.session_id
            )
        self._user_initiated_close = False
        self._msg_id_cache: collections.OrderedDict[int, str] = collections.OrderedDict()
        # Resident idle listener: drains unsolicited (background-task) turns
        # between sends so they never rot in the stdout queue and mispair.
        self._listener_stop = asyncio.Event()
        # Cortex shell host (T9), attached by __main__ when shell_active().
        # None = plain relay resident, every shell branch below is skipped.
        self._shell = None
        # Shell receipts (💤 / 🌙 / 🔄) held until the running reply cycle has
        # shipped its text, so they land under the reply instead of above it.
        self._pending_notices: list[str] = []
        self._notice_defer = 0
        # A transfer(rotate=True) owns the rotation it triggers: its combined
        # receipt is already queued, so the rotate path must not add a 🌙.
        self._transfer_rotate_pending = False

    def attach_shell(self, shell) -> None:
        self._shell = shell

    def attach_bot(self, bot) -> None:
        """Seed the outbound bot at startup. Without it every bridge-initiated
        round (shell note, unsolicited turn) is stuck until the user speaks
        first, because self._bot is otherwise only learned from an inbound
        message."""
        if self._bot is None:
            self._bot = bot

    def _outbound_target(self) -> tuple["Bot | None", int | None]:
        """Where a bridge-initiated round ships: the live chat once one is
        known, else the configured [tg].chat_id — a freshly restarted bridge
        has no inbound message yet."""
        chat_id = self._pending_chat_id
        if chat_id is None:
            chat_id = self._cfg.chat_id
        return self._bot, chat_id

    def _load_state(self) -> BridgeState:
        state = BridgeState()
        saved = bridge_state_store.load(self._state_path)
        for k, v in saved.items():
            if hasattr(state, k):
                setattr(state, k, v)
        # A saved /model switch is the new default and wins. default_model only
        # seeds a bridge that never switched; empty → None, not "", so it
        # matches the "no model known yet" sentinel used everywhere else
        # (provider skips --model, display_name shows "?").
        if not state.model:
            state.model = self._cfg.default_model or None
        return state

    def _persist_state(self) -> None:
        bridge_state_store.save(self._state_path, asdict(self._state))

    def _swap_provider(self, model: str | None, sid: str | None) -> None:
        if self._provider:
            self._user_initiated_close = True
            try:
                self._provider.cancel()
            except Exception:
                pass
        if model is not None:
            self._state.model = model
        if sid is not None:
            self._state.session_id = sid
        self._provider = self._make_provider()
        self._provider.spawn()
        logger.info("swap_provider: respawned (model=%s, sid=%s)", self._state.model, sid)
        if sid:
            created = get_session_created_at(self._cfg.session_created_command, sid)
            if created:
                self._session_created_at = created
        else:
            from datetime import datetime, timezone
            self._session_created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._state.usage_total = {}
        self._state.last_assistant_usage = {}

    def _close_provider(self) -> None:
        if self._provider:
            try:
                self._provider.close()
            except Exception:
                pass
            self._provider = None

    def _forget_session(self) -> None:
        self._state.session_id = None
        self._death_count = 0
        self._buffer = InboundBuffer()
        self._pending_media = []
        if self._sessions is not None:
            for cid in list(self._sessions.snapshot()):
                self._sessions.forget(cid)

    def _record_effort(self, sid: str, effort: str) -> None:
        try:
            subprocess.run(
                ["mw", "add-session", "--sid", sid, "--effort", effort],
                capture_output=True, timeout=5.0,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("record_effort failed: %s", e)

    _MARROW_PY = os.environ.get(
        "MARROW_PYTHON",
        str(Path.home() / "CC-Lab/marrow/.venv/bin/python"),
    )
    _DIARY_SCRIPT = "\n".join([
        "import sys,json",
        "from datetime import datetime,timedelta",
        "from marrow.config import get_tz",
        "from marrow.timecue import parse_time_cue",
        "from marrow.daemon import recall",
        "_m=get_tz()",
        "cue=parse_time_cue(sys.stdin.read().strip(),datetime.now(_m))",
        "if not cue:print('null');sys.exit(0)",
        "s=datetime.fromisoformat(cue.since_utc).astimezone(_m).strftime('%Y-%m-%d')",
        "u=(datetime.fromisoformat(cue.until_utc).astimezone(_m)-timedelta(days=1)).strftime('%Y-%m-%d')",
        "ds=[dict(c=r['content'],d=r.get('date',''))for r in recall(query='diary',since=s,until=u,limit=5)if r.get('kind')=='diary']",
        "print(json.dumps(ds or None))",
    ])

    def _make_fetch_diary(self) -> Callable[[str], tuple[str | None, str | None]]:
        def _fetch(raw_date: str) -> tuple[str | None, str | None]:
            try:
                proc = subprocess.run(
                    [TgLoop._MARROW_PY, "-c", TgLoop._DIARY_SCRIPT],
                    input=raw_date, capture_output=True, text=True, timeout=15,
                )
                if proc.returncode != 0:
                    return (None, None)
                data = json.loads(proc.stdout.strip())
                if not data:
                    return (None, None)
                content = "\n---\n".join(d["c"] for d in data)
                label = data[0]["d"] or raw_date
                return (content, label)
            except Exception:
                return (None, None)
        return _fetch

    def _build_registry(self) -> Registry:
        ctx = CommandContext(
            state=self._state,
            swap_provider=self._swap_provider,
            close_provider=self._close_provider,
            forget_session=self._forget_session,
            persist_state=self._persist_state,
            # Empty on purpose: default_model only SEEDS a never-switched
            # bridge (_load_state). /clear must follow the saved state.model
            # so a /model switch survives it.
            clear_default_model="",
            commands_doc_path=Path(__file__).resolve().parents[1] / "COMMANDS.md",
            fetch_diary=self._make_fetch_diary(),
            record_effort=self._record_effort,
            resolve_session_effort=lambda sid: get_session_effort(
                self._cfg.session_get_effort_command, sid
            ),
        )
        return Registry(ctx)

    def _shell_fold(self, resume_sid: str | None) -> None:
        """Window-end choke point for the cortex shell's today ledger. Every
        provider spawn — fuse respawn, rotate, /clear, /resume, /cwd,
        cross-channel takeover, crash respawn — goes through _make_provider, so
        folding here catches them all; resuming the ledger's own session is a
        no-op, and the fold itself is idempotent. No-op for a plain relay
        resident; never raises into the turn path."""
        if self._shell is None:
            return
        try:
            self._shell.fold_session(resume_sid)
        except Exception as e:  # noqa: BLE001 — ledger bookkeeping is best-effort
            logger.warning("shell fold_session failed: %s", e)

    def _make_provider(self) -> ClaudeCodeProvider:
        cfg = self._cfg
        state = self._state
        self._shell_fold(state.session_id)
        return ClaudeCodeProvider(
            model=state.model,
            resume_sid=state.session_id,
            binary=cfg.cc_path,
            cwd=state.cc_cwd or (str(cfg.cwd) if cfg.cwd else None),
            channel="tg",
            marrow_bridge=cfg.marrow_bridge,
            effort_level=state.effort_level,
            stderr_log=Path.home() / "Library/Logs/synapse-tg-cc-stderr.log",
            system_prompts=[QUOTE_SYSTEM_PROMPT, MEDIA_SYSTEM_PROMPT, TG_BUBBLE_FORMAT_PROMPT, NIGHT_SYSTEM_PROMPT],
            idle_soft_s=cfg.idle_soft_s,
            idle_hard_s=cfg.idle_hard_s,
            turn_output_cap=cfg.turn_output_cap,
            # Cortex shell id — marrow reads MARROW_CORTEX to decide whether
            # this resident gets the cortex tools/hooks (T8). Gate: T7 single
            # source (marrow's [cortex].shells), no local enable flag.
            extra_env={"MARROW_CORTEX": cfg.shell_id} if cfg.shell_active() else None,
        )

    def ensure_provider(self) -> None:
        if self._death_count >= _MAX_CONSECUTIVE_DEATHS:
            self._provider = None
            return
        if self._provider is None or not self._provider.is_alive():
            self._provider = self._make_provider()
            self._provider.spawn()
            logger.info("provider spawned (sid=%s)", self._provider.session_id)

    def _respawn(self) -> None:
        self._death_count += 1
        if self._death_count >= _MAX_CONSECUTIVE_DEATHS:
            logger.error("provider died %d times, backing off", self._death_count)
            return
        logger.warning("provider dead — respawning (%d/%d)", self._death_count, _MAX_CONSECUTIVE_DEATHS)
        try:
            if self._provider:
                self._provider.cancel()
        except Exception:
            pass
        self._provider = self._make_provider()
        self._provider.spawn()

    def _drain_recv(self) -> tuple[str, str]:
        """Drain provider response (kept for reference). Returns (text, thinking)."""
        assert self._provider is not None
        chunks: list[str] = []
        thinking: list[str] = []
        for ev in self._provider.recv():
            t = ev.get("type")
            if t == "system":
                if ev.get("subtype") == "init":
                    sid = ev.get("session_id")
                    if sid and isinstance(sid, str):
                        if self._state.session_id != sid:
                            self._state.session_id = sid
                            self._session_created_at = get_session_created_at(
                                self._cfg.session_created_command, sid
                            ) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            self._persist_state()
                        elif not self._session_created_at:
                            self._session_created_at = get_session_created_at(
                                self._cfg.session_created_command, sid
                            ) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        if self._sessions is not None and self._pending_chat_id is not None:
                            self._sessions.set(str(self._pending_chat_id), sid)
                        if self._record_session is not None:
                            try:
                                self._record_session(sid, self._state.model)
                            except Exception:
                                logger.warning("record_session failed for %s", sid)
                continue
            if t == "assistant":
                msg = ev.get("message") or {}
                for block in msg.get("content", []):
                    bt = block.get("type")
                    if bt == "text":
                        cleaned = strip_tool_xml(block["text"])
                        if cleaned:
                            chunks.append(cleaned)
                    elif bt == "thinking":
                        if block.get("thinking"):
                            thinking.append(block["thinking"])
            elif t == "result":
                break
        self._death_count = 0
        return "\n\n".join(chunks), "\n".join(thinking)

    def _handle_init_event(self, ev: dict) -> None:
        """Shared system(init) handling: adopt session_id, stamp created_at,
        record the session. Used by every turn (solicited + unsolicited)."""
        # cc reports the model it actually resolved (state.model may be a
        # floating alias like "opus"). Display-only — mirrored onto the
        # provider because the idle listener parses init events outside recv().
        model = ev.get("model")
        if isinstance(model, str) and model and self._provider is not None:
            self._provider.model_actual = model
            token = self._provider.model or self._state.model
            if remember_resolved_model(self._state, token, model):
                self._persist_state()
        sid = ev.get("session_id")
        if not (sid and isinstance(sid, str)):
            return
        if self._state.session_id != sid:
            self._state.session_id = sid
            self._session_created_at = get_session_created_at(
                self._cfg.session_created_command, sid
            ) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._persist_state()
        elif not self._session_created_at:
            self._session_created_at = get_session_created_at(
                self._cfg.session_created_command, sid
            ) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self._sessions is not None and self._pending_chat_id is not None:
            self._sessions.set(str(self._pending_chat_id), sid)
        if self._record_session is not None:
            try:
                self._record_session(sid, self._state.model)
            except Exception:
                logger.warning("record_session failed for %s", sid)

    async def _collect_turn(
        self, typing: TypingAction, first_line: str | None = None,
        bot: Bot | None = None, chat_id: int | None = None,
    ) -> tuple[str, str, bool] | None:
        """Drain ONE turn from the provider. Returns (text, thinking,
        unsolicited) or None when the recv thread ended before any turn
        (clean EOF between turns). Raises on provider death mid-turn.

        `first_line` is a raw line the idle listener already pulled off the
        queue that opened this turn; recv processes it before the queue.
        `bot`/`chat_id` (when known) let a lie_down(rotate=False) or transfer
        tool_use queue its receipt for the end of the reply cycle.
        """
        assert self._provider is not None
        q: queue.Queue = queue.Queue()
        t = threading.Thread(
            target=_recv_to_queue,
            args=(self._provider, q, first_line),
            daemon=True,
        )
        t.start()

        text_chunks: list[str] = []
        thinking_chunks: list[str] = []
        unsolicited = False
        first_event = True
        completed = False
        loop = asyncio.get_event_loop()

        while True:
            ev = await loop.run_in_executor(None, q.get)
            if ev is None:
                break
            if ev is _TURN_END:
                completed = True
                continue
            if isinstance(ev, Exception):
                raise ev

            if first_event:
                unsolicited = _is_unsolicited_first_event(ev)
                first_event = False

            t_type = ev.get("type")
            if t_type == "system":
                if ev.get("subtype") == "init":
                    self._handle_init_event(ev)
                # task_notification and other system frames yield no text.
                continue
            if t_type == "assistant":
                msg = ev.get("message") or {}
                for block in msg.get("content", []):
                    bt = block.get("type")
                    if bt == "text":
                        chunk = strip_tool_xml(block.get("text", ""))
                        if chunk:
                            text_chunks.append(chunk)
                    elif bt == "tool_use":
                        if not typing.running:
                            typing.start()
                        name = block.get("name") or ""
                        if name.endswith("lie_down"):
                            tool_input = block.get("input") or {}
                            if not tool_input.get("rotate"):
                                self._queue_lie_down(bot, chat_id, tool_input)
                        elif name.endswith("transfer"):
                            self._queue_transfer(bot, chat_id, block.get("input") or {})
                    elif bt == "thinking":
                        # cc fills BOTH the stream_event thinking_delta path
                        # and this final-frame thinking block with the same
                        # plaintext under --include-partial-messages. Reading
                        # both would duplicate the 🧠 bubble; stream_event is
                        # the source of truth — skip here.
                        pass
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    self._merge_usage(usage)
                    snap = {k: v for k, v in usage.items() if isinstance(v, int)}
                    if snap:
                        self._state.last_assistant_usage = snap
            elif t_type == "stream_event":
                # cc --include-partial-messages forwards SSE deltas as
                # `stream_event` frames. Under OAuth the final assistant
                # `thinking` block is empty (signature-only); the plaintext
                # only lives in these in-flight `thinking_delta` chunks.
                e = ev.get("event") or {}
                if e.get("type") == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "thinking_delta":
                        txt = d.get("thinking")
                        if isinstance(txt, str) and txt:
                            thinking_chunks.append(txt)
            elif t_type == "result":
                usage = ev.get("usage")
                if isinstance(usage, dict):
                    self._merge_usage(usage)

        if not completed and first_event:
            # Thread ended with no events at all (clean EOF between turns).
            return None
        self._death_count = 0
        return "\n\n".join(text_chunks), "".join(thinking_chunks), unsolicited

    async def _stream_response(
        self, bot: Bot, chat_id: int, typing: TypingAction
    ) -> tuple[str, str]:
        """Drain provider turns until the first solicited reply turn.

        Any unsolicited turn (background task completion) seen before the
        solicited reply is delivered immediately via _deliver_reply, then
        collection continues. Returns the solicited turn's (text, thinking).
        """
        assert self._provider is not None
        unsolicited_count = 0
        while True:
            turn = await self._collect_turn(typing, bot=bot, chat_id=chat_id)
            if turn is None:
                return "", ""
            text, thinking, unsolicited = turn
            if not unsolicited:
                return text, thinking
            unsolicited_count += 1
            self._maybe_storm_alert(unsolicited_count)
            await self._deliver_reply(bot, chat_id, text, thinking)

    async def _listen_once(self) -> None:
        """One idle-listener iteration. Polls INSIDE the flush lock so it can
        never overlap a send; sleeps OUTSIDE it (in _idle_listener) so a
        pending check_flush can win the lock. Re-reads self._provider fresh —
        never caches it (a slash-command swap replaces the object without
        holding this lock)."""
        provider = self._provider
        if provider is None or not getattr(provider, "alive", False):
            return  # nothing to drain; lazy respawn happens on the next send
        bot, chat_id = self._outbound_target()

        async with self._lock:
            # Re-read after acquiring: a swap may have replaced it while waiting.
            provider = self._provider
            if provider is None or not getattr(provider, "alive", False):
                return
            line = await asyncio.to_thread(provider.poll_line, _LISTEN_POLL_TIMEOUT_SEC)
            if line is None:
                return
            if line is POLL_EOF:
                logger.info("idle listener: provider EOF — marked dead, awaiting respawn")
                provider.alive = False
                return
            # Classify BEFORE reacting: only a line whose event opens a genuine
            # unsolicited turn may start typing and enter the blocking drain.
            if self._consume_non_turn_line(line):
                return
            # A real unsolicited turn is arriving. Target the last real chat;
            # if none, drop with a warning (never crash).
            if bot is None or chat_id is None:
                logger.warning("idle listener: unsolicited turn with no chat target — dropped")
                # Still drain the turn so it doesn't rot in the queue.
                await self._collect_turn(_NullTyping(), first_line=line)
                return
            typing = TypingAction(bot, chat_id)
            typing.start()
            try:
                await self._drain_unsolicited(bot, chat_id, typing, line)
            finally:
                typing.stop()

    def _consume_non_turn_line(self, line: str) -> bool:
        """Classify a first polled line; True when it opens NO turn and was
        consumed here (no typing, no blocking recv).

        Every cc spawn (fresh or --resume, e.g. shell_respawn / /model) emits a
        system{init} handshake as its first stdout line. It carries no result
        event, so feeding it to _collect_turn blocks recv until idle_hard_s and
        then SIGKILLs the fresh process — while typing runs the whole time.
        Handle the handshake's state here instead; only a task_notification-first
        line is a real unsolicited turn."""
        stripped = line.strip()
        if not stripped:
            return True
        try:
            ev = json.loads(stripped)
        except ValueError:
            logger.warning("idle listener: skip non-json line: %s", line[:120])
            return True
        if not isinstance(ev, dict):
            logger.warning("idle listener: skip non-object line: %s", line[:120])
            return True
        if _is_unsolicited_first_event(ev):
            return False
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            self._handle_init_event(ev)
            logger.info(
                "idle listener: consumed spawn handshake (sid=%s)",
                ev.get("session_id"),
            )
        else:
            logger.warning(
                "idle listener: dropped non-turn first event (type=%s subtype=%s)",
                ev.get("type"), ev.get("subtype"),
            )
        return True

    async def _drain_unsolicited(
        self, bot: Bot, chat_id: int, typing: TypingAction, first_line: str
    ) -> None:
        """Collect and deliver the unsolicited turn opened by first_line, plus
        any consecutive back-to-back turns already queued behind it. Each queued
        line is classified too: a non-turn line (spawn handshake) is consumed
        without entering the blocking drain. Shell receipts raised by any of
        those turns ship once, after the last one's text."""
        count = 0
        line: str | None = first_line
        self._notice_defer += 1
        try:
            while line is not None:
                if not self._consume_non_turn_line(line):
                    turn = await self._collect_turn(typing, first_line=line, bot=bot, chat_id=chat_id)
                    if turn is not None:
                        text, thinking, _unsolicited = turn
                        count += 1
                        self._maybe_storm_alert(count)
                        await self._deliver_reply(bot, chat_id, text, thinking)
                # Peek for the next queued turn without blocking on idle liveness.
                provider = self._provider
                if provider is None or not getattr(provider, "alive", False):
                    break
                nxt = provider.poll_line(0.0)
                if nxt is None or nxt is POLL_EOF:
                    if nxt is POLL_EOF:
                        provider.alive = False
                    break
                line = nxt
        finally:
            self._notice_defer -= 1
            await self._flush_notices(bot, chat_id)

    async def _idle_listener(self) -> None:
        """Resident task: drain unsolicited turns between sends for the life of
        the bridge. Never dies from an exception (catch-all -> log -> continue).
        Stops on _listener_stop (clean shutdown)."""
        logger.info("idle listener started")
        while not self._listener_stop.is_set():
            try:
                await self._listen_once()
            except Exception as e:  # never let the listener die
                logger.warning("idle listener iteration error: %s", e)
            # Sleep OUTSIDE the lock so a pending check_flush wins it (FIFO
            # waiters + this window).
            try:
                await asyncio.wait_for(
                    self._listener_stop.wait(), timeout=_LISTEN_RELEASE_SLEEP_SEC
                )
            except asyncio.TimeoutError:
                pass
        logger.info("idle listener stopped")

    def stop_listener(self) -> None:
        self._listener_stop.set()

    def _merge_usage(self, usage: dict[str, Any]) -> None:
        for k, v in usage.items():
            if isinstance(v, int):
                self._state.usage_total[k] = self._state.usage_total.get(k, 0) + v

    def _local_hhmm_plus(self, minutes: float = 0.0) -> str:
        """Now + `minutes`, rendered HH:mm in [core].timezone."""
        now = datetime.now(self._tz)
        return (now + timedelta(minutes=minutes)).strftime("%H:%M")

    def _queue_lie_down(
        self, bot: Bot | None, chat_id: int | None, tool_input: dict
    ) -> None:
        """lie_down(rotate=False) tool_use: queue the 💤 receipt for the end of
        the reply cycle. No-op without a known chat target."""
        if bot is None or chat_id is None:
            return
        try:
            mins = int(float(tool_input.get("next_wake_min")))
        except (TypeError, ValueError):
            logger.warning("lie_down notice: bad next_wake_min %r",
                           tool_input.get("next_wake_min"))
            return
        self._pending_notices.append(
            messages.t("shell.lie_down", self._state.voice_style,
                       min=mins, time=self._local_hhmm_plus(mins))
        )

    def _queue_transfer(
        self, bot: Bot | None, chat_id: int | None, tool_input: dict
    ) -> None:
        """transfer tool_use: queue the 🔄 receipt for the end of the reply
        cycle. transfer(rotate=True) gets ONE combined receipt and claims the
        rotation, so shell_rotate stays silent for it. No-op without a known
        chat target."""
        if bot is None or chat_id is None:
            return
        rotate = bool(tool_input.get("rotate"))
        if rotate:
            self._transfer_rotate_pending = True
        key = "shell.transferred_rotated" if rotate else "shell.transferred"
        self._pending_notices.append(
            messages.t(key, self._state.voice_style, shell=self._cfg.shell_peer())
        )

    async def _send_notice(self, bot: Bot, chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning("shell notice send failed: %s", e)

    async def _emit_notice(self, bot: Bot, chat_id: int, text: str) -> None:
        """Ship a shell receipt: held back while a reply cycle is running,
        sent right away when none is."""
        if self._notice_defer > 0:
            self._pending_notices.append(text)
            return
        await self._send_notice(bot, chat_id, text)

    async def _flush_notices(self, bot: Bot | None, chat_id: int | None) -> None:
        """Ship every queued receipt, once, after the cycle's reply text.
        Drops the queue when there is no chat target to ship to."""
        if not self._pending_notices:
            return
        pending, self._pending_notices = self._pending_notices, []
        if bot is None or chat_id is None:
            logger.warning("shell notices dropped — no chat target")
            return
        for text in pending:
            await self._send_notice(bot, chat_id, text)

    def _maybe_storm_alert(self, count: int) -> None:
        """More than unsolicited_storm_cap unsolicited turns in one lock-hold
        signals the CLI protocol may have started mispairing again. Alert once
        (at cap+1) + log ERROR; delivery keeps going regardless."""
        cap = self._cfg.unsolicited_storm_cap
        if cap <= 0 or count != cap + 1:
            return
        logger.error(
            "unsolicited turn storm: %d turns in one lock-hold (cap %d)",
            count, cap,
        )
        if self._alerts is not None:
            try:
                self._alerts.write(
                    "warn", "bridge_turn_storm",
                    f"{count} unsolicited turns delivered in one lock-hold "
                    f"(cap {cap}) — possible CLI mispairing",
                    source="loop.stream",
                    fingerprint="bridge_turn_storm",
                )
            except Exception as ae:
                logger.warning("alerts.write failed: %s", ae)

    async def _send_provider_notice(self, bot: Bot, chat_id: int, key: str) -> None:
        try:
            await bot.send_message(chat_id=chat_id, text=messages.t(key, self._state.voice_style))
        except Exception as e:
            logger.warning("provider notice send failed (%s): %s", key, e)

    def idle_close_provider(self, sid: str) -> None:
        """Called by IdleFireLoop pre_spawn_hook. Graceful close if sids match."""
        if self._provider is None:
            return
        if sid and self._state.session_id and sid != self._state.session_id:
            return
        try:
            self._provider.close()
        except Exception as e:
            logger.warning("idle provider close failed: %s", e)
        self._provider = None

    def respawn_with_resume(self, sid: str, model: str | None) -> None:
        """Close current provider and spawn fresh with --resume.

        If the session was killed and its index entry removed from
        ~/.claude/sessions/, fallback to --create instead.
        """
        if self._provider is not None:
            self._user_initiated_close = True
            try:
                # Suppress intermediate SessionEnd so regen/rewind doesn't archive truncated jsonl.
                _suppress = regen_suppress_path(sid)
                try:
                    _suppress.touch(exist_ok=True)
                except OSError:
                    pass
                try:
                    self._provider.close()
                except Exception:
                    pass
                self._provider = None
            finally:
                self._user_initiated_close = False
        self._death_count = 0

        # Check if the session jsonl still exists on disk. The session
        # index (~/.claude/sessions/*.json) is cleaned up on graceful exit,
        # so checking it always gives false after close(). The jsonl file
        # is what cc --resume actually needs.
        use_resume = True
        try:
            from synapse_core.jsonl_edit import _jsonl_path
            jsonl = _jsonl_path(sid, cwd=self._cfg.cwd and str(self._cfg.cwd), projects_root=None)
            if not jsonl:
                logger.warning("session %s jsonl not found, fallback to --create", sid[:8])
                use_resume = False
        except Exception as e:
            logger.warning("failed to locate session jsonl: %s", e)

        self._state.session_id = sid
        if model:
            self._state.model = model
        self._provider = self._make_provider()
        # Override resume_sid if fallback to --create is needed.
        if not use_resume:
            self._provider.resume_sid = None
        self._provider.spawn()
        self._state.usage_total = {}
        self._state.last_assistant_usage = {}
        logger.info("respawn_with_resume sid=%s model=%s (resume=%s)", sid, model, use_resume)

    def replay_user_text(self, text: str) -> None:
        """Enqueue text on the inbound buffer for the next flush cycle."""
        self._buffer.add(text)

    def get_status(self) -> dict:
        """Return current bridge status for /info display."""
        alive = self._provider is not None and self._provider.alive
        age = None
        if self._session_created_at:
            try:
                created = datetime.fromisoformat(self._session_created_at.replace("Z", "+00:00"))
                age = (datetime.now(timezone.utc) - created).total_seconds()
            except (ValueError, TypeError):
                pass
        return {
            "model": self._state.model,
            "model_actual": getattr(self._provider, "model_actual", None),
            "session_id": self._state.session_id,
            "effort": self._state.effort_level,
            "thinking": self._state.thinking_on,
            "quote": self._state.quote_on,
            "voice_style": self._state.voice_style,
            "cwd": self._state.cc_cwd or (str(self._cfg.cwd) if self._cfg.cwd else None),
            "provider_alive": alive,
            "ilink_ok": True,
            "cc_pid": id(self._provider) if alive else None,
            "session_age_sec": age,
        }

    async def send_extra_bubbles(self, bubbles: list[str]) -> None:
        """Send replay/extra bubbles to the current TG chat."""
        if self._bot is None or self._pending_chat_id is None:
            return
        for text in bubbles:
            try:
                await self._bot.send_message(chat_id=self._pending_chat_id, text=text)
                await asyncio.sleep(_SEND_GAP_SEC)
            except Exception as e:
                logger.warning("send_extra_bubbles failed: %s", e)

    def _track(self, bot: Bot, chat_id: int, count_activity: bool = True) -> None:
        self._bot = bot
        self._pending_chat_id = chat_id
        # An inbound message restarts the cortex shell's silence cycle and
        # cancels a booked wake only when it actually reaches the LLM. Text
        # turns count a "forward" verdict or an injected rewrite; anything the
        # registry consumes without injecting leaves a scheduled wake standing.
        # Media turns always count.
        if count_activity and self._shell is not None:
            self._shell.on_user_message()

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.message.text is None:
            return
        text = update.message.text.strip()
        if not text:
            return
        # Dispatch before _track: its verdict plus any pending rewrite tells a
        # message that feeds the LLM apart from one the registry only consumes.
        # Nothing inside dispatch reads the bot/chat_id _track binds. Both are
        # synchronous, so no other task can observe the gap.
        action, ack = self._registry.dispatch(text)
        inject = self._registry.pending_rewrite
        self._track(context.bot, update.message.chat_id,
                     count_activity=(action == "forward" or bool(inject)))

        if action == "handled":
            if self._queued_extra_bubbles:
                bubbles = self._queued_extra_bubbles[:]
                self._queued_extra_bubbles.clear()
                for b in bubbles:
                    try:
                        await context.bot.send_message(chat_id=update.message.chat_id, text=b)
                        await asyncio.sleep(_SEND_GAP_SEC)
                    except Exception:
                        pass
            if ack and update.message:
                await update.message.reply_text(ack)
            if inject:
                self._buffer.add(inject)
            return

        quote_prefix = ""
        reply = update.message.reply_to_message
        if reply and reply.text:
            quoted = reply.text[:80]
            quote_prefix = f'[quoting: "{quoted}"]\n'
        self._buffer.add(f"{quote_prefix}{text}" if quote_prefix else text)
        logger.info("inbound: %r (len=%d)", text[:60], len(text))
        if update.message:
            self._msg_id_cache[update.message.message_id] = text
            if len(self._msg_id_cache) > 50:
                self._msg_id_cache.popitem(last=False)

    def _queue_media(self, kind: str, message: "Message") -> None:
        """Reserve this media's place in the inbound buffer, download later.

        Runs to completion before the handler's first await, so check_flush
        cannot slip in between the message arriving and the buffer slot being
        taken — a text bubble sent seconds earlier stays in the same turn.
        Caption (or sticker meta) goes in now; the Read instruction is appended
        at flush time once the file is on disk.
        """
        if kind == "sticker":
            stk = message.sticker
            lead = f"[sticker: emoji={stk.emoji or '?'}, set={stk.set_name or 'none'}]"
        else:
            lead = (message.caption or "").strip()
        self._pending_media.append((kind, message))
        self._buffer.add(lead or _MEDIA_SENTINEL)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.photo:
            return
        self._track(context.bot, update.message.chat_id)
        self._queue_media("photo", update.message)

    async def on_animation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.animation:
            return
        self._track(context.bot, update.message.chat_id)
        self._queue_media("animation", update.message)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.document:
            return
        self._track(context.bot, update.message.chat_id)
        self._queue_media("document", update.message)

    async def on_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.sticker:
            return
        self._track(context.bot, update.message.chat_id)
        self._queue_media("sticker", update.message)

    async def on_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.video:
            return
        self._track(context.bot, update.message.chat_id)
        self._queue_media("video", update.message)

    async def _materialize_pending(
        self, bot: Bot, pending: list[tuple[str, "Message"]]
    ) -> list[str]:
        """Download a drained media snapshot → one Read instruction per item.

        Network IO, so it runs after the buffer snapshot, never inside it. A
        download that raises costs its own line only: the rest of the turn
        still ships.
        """
        lines: list[str] = []
        for kind, message in pending:
            try:
                paths = await self._download_media(bot, kind, message)
            except Exception as e:
                logger.warning("media download failed (%s): %s", kind, e)
                lines.append(_MEDIA_FAILED_LINE)
                continue
            if paths:
                lines.append(build_read_instruction(paths))
                logger.debug("buffered %s: %s", kind, paths)
        return lines

    async def _download_media(self, bot: Bot, kind: str, message: "Message") -> list[Path]:
        data_dir = self._cfg.data_dir
        if kind == "photo":
            return list(await materialize_photo(bot, message, data_dir))
        materialize = {
            "animation": materialize_animation,
            "document": materialize_document,
            "sticker": materialize_sticker,
            "video": materialize_video,
        }[kind]
        path = await materialize(bot, message, data_dir)
        return [path] if path else []

    async def _send_text_bubble(self, bot: Bot, send_kwargs: dict, fallback_kwargs: dict) -> bool:
        """Send one text bubble with 429 RetryAfter handling and a plain-text
        fallback. Returns True on success, False if the bubble was lost.

        Never raises: a fallback failure is caught so it cannot kill the turn.
        """

        async def _attempt(kwargs: dict) -> bool:
            attempts = max(1, self._cfg.send_retry_max)
            for i in range(attempts):
                try:
                    await bot.send_message(**kwargs)
                    return True
                except RetryAfter as e:
                    wait = float(getattr(e, "retry_after", 0)) or 0.0
                    if wait > self._cfg.retry_after_cap_sec or i == attempts - 1:
                        raise
                    await asyncio.sleep(wait + _RETRY_AFTER_MARGIN_SEC)
            return False

        try:
            return await _attempt(send_kwargs)
        except Exception as e:
            logger.warning("send_message failed, trying plain-text fallback: %s", e)
        try:
            return await _attempt(fallback_kwargs)
        except Exception as e:
            logger.warning("plain-text fallback send also failed: %s", e)
            return False

    async def check_flush(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._buffer.ready() or self._pending_chat_id is None:
            return
        bot = self._bot or context.bot
        chat_id = self._pending_chat_id
        body = self._buffer.flush()
        pending = self._pending_media
        self._pending_media = []
        if pending:
            kept = [ln for ln in body.split("\n") if ln != _MEDIA_SENTINEL] if body else []
            instructions = await self._materialize_pending(bot, pending)
            body = "\n".join(kept + instructions)
        if not body:
            return

        logger.info("flush: %r", body[:120])
        typing = TypingAction(bot, chat_id)

        # Shell receipts raised anywhere in this cycle (💤 / 🔄 tool_use, a
        # rotate kick landing mid-turn) wait for the finally below, so they
        # land under the reply text instead of above it.
        self._notice_defer += 1
        try:
            async with self._lock:
                try:
                    # Retry-once: on a mid-turn stall/death, respawn resuming the
                    # same sid and re-send the SAME body ONCE. Second failure ->
                    # user-facing notice. Bridges emit outbound only from completed
                    # events, so a retried turn double-sends nothing.
                    response = thinking = None
                    for attempt in range(2):
                        try:
                            self.ensure_provider()
                            assert self._provider is not None
                            typing.start()
                            await asyncio.to_thread(self._provider.send, body)
                            response, thinking = await self._stream_response(bot, chat_id, typing)
                            if self._provider and self._provider.session_id:
                                if self._state.session_id != self._provider.session_id:
                                    self._state.session_id = self._provider.session_id
                                    self._persist_state()
                            break
                        except ProviderDeadError as e:
                            if self._user_initiated_close:
                                self._user_initiated_close = False
                                return
                            logger.error("provider error (attempt %d/2): %s", attempt + 1, e)
                            self._respawn()
                            if self._death_count >= _MAX_CONSECUTIVE_DEATHS:
                                logger.error("provider gave up after %d consecutive deaths", self._death_count)
                                self._provider = None
                                await self._send_provider_notice(bot, chat_id, "provider.gave_up")
                                return
                            if attempt == 0:
                                continue
                            # Second failure: hand back to the buffer + notice.
                            self._buffer.prepend(body)
                            await self._send_provider_notice(bot, chat_id, "provider.restarting")
                            return
                except Exception as e:
                    logger.error("unexpected error: %s", e)
                    await bot.send_message(chat_id=chat_id, text=messages.t("bridge.error", self._state.voice_style))
                    return
                finally:
                    typing.stop()

            # Turn output cap: the provider interrupted a runaway turn (brake, not
            # a failure — no retry). Notify the user; the partial reply below still
            # ships. Notice fires once per capped turn.
            if self._provider is not None and getattr(
                self._provider, "turn_output_capped", False
            ):
                await self._send_provider_notice(bot, chat_id, "provider.turn_capped")

            # Reply always ships. Messages that arrived mid-turn stayed in the
            # InboundBuffer (never drained) and become the next turn — no merge,
            # no reply-drop.
            await self._deliver_reply(bot, chat_id, response, thinking)
        finally:
            self._notice_defer -= 1
            await self._flush_notices(bot, chat_id)
        await self._shell_after_turn()

    async def _shell_after_turn(self) -> None:
        """Hand a completed turn to the cortex shell host (token ledger + fuse).
        No-op for a plain relay resident; never raises into the turn path."""
        if self._shell is None:
            return
        try:
            await self._shell.after_turn()
        except Exception as e:  # noqa: BLE001 — shell bookkeeping is best-effort
            logger.warning("shell after_turn failed: %s", e)

    async def feed_turn(self, body: str) -> bool:
        """Feed one machine turn (note / fuse prompt) into the resident session
        and ship its reply to tg like any other turn — free-round replies are
        never held. Returns False when there is no chat target or the provider
        could not take the turn."""
        bot, chat_id = self._outbound_target()
        if bot is None or chat_id is None:
            logger.warning("feed_turn: no chat target — round skipped")
            return False
        typing = TypingAction(bot, chat_id)
        self._notice_defer += 1
        try:
            async with self._lock:
                try:
                    self.ensure_provider()
                    if self._provider is None:
                        logger.warning("feed_turn: no provider — round skipped")
                        return False
                    typing.start()
                    await asyncio.to_thread(self._provider.send, body)
                    response, thinking = await self._stream_response(bot, chat_id, typing)
                except Exception as e:
                    logger.warning("feed_turn failed: %s", e)
                    return False
                finally:
                    typing.stop()
            await self._deliver_reply(bot, chat_id, response, thinking)
            return True
        finally:
            self._notice_defer -= 1
            await self._flush_notices(bot, chat_id)

    def shell_respawn(self) -> None:
        """Drop the resident session and spawn a fresh one (fuse). Queued user
        messages stay on the InboundBuffer and land in the new session."""
        self._state.session_id = None
        self._session_created_at = None
        if self._sessions is not None:
            for cid in list(self._sessions.snapshot()):
                self._sessions.forget(cid)
        self._persist_state()
        self._swap_provider(None, None)
        logger.info("shell respawn: fresh session")

    async def shell_rotate(self, wake: float | None = None) -> None:
        """lie_down(rotate=True) from the shell: let any in-flight turn finish,
        then drop the session for a fresh one. The lock is what the fuse path
        gets for free by respawning after its turn — a rotate kick can land
        mid-turn. This is the truthful rotation signal (a rotate tool_use
        alone can be denied by a marrow hook) — send the 🌙 notice here, and
        carry the booked wake (`wake`, epoch seconds) when there is one. A
        rotation a transfer asked for is already covered by the 🔄 receipt:
        one action, one receipt."""
        async with self._lock:
            self.shell_respawn()
        transfer_owned = self._transfer_rotate_pending
        self._transfer_rotate_pending = False
        if transfer_owned:
            return
        bot, chat_id = self._outbound_target()
        if bot is None or chat_id is None:
            return
        mins = 0
        if wake is not None:
            mins = round((wake - datetime.now(self._tz).timestamp()) / 60)
        if mins <= 0:
            text = messages.t("shell.rotated", self._state.voice_style)
        else:
            text = messages.t(
                "shell.rotated_wake", self._state.voice_style, min=mins,
                time=datetime.fromtimestamp(wake, self._tz).strftime("%H:%M"))
        await self._emit_notice(bot, chat_id, text)

    def _resolve_quote_target(self, fragment: str) -> int | None:
        """Newest cached inbound message containing FRAGMENT, or None."""
        if not fragment:
            return None
        for msg_id, msg_text in reversed(self._msg_id_cache.items()):
            if fragment.lower() in msg_text.lower():
                return msg_id
        return None

    async def _deliver_reply(
        self, bot: Bot, chat_id: int, response: str, thinking: str
    ) -> None:
        """Send one completed turn (thinking blockquote + quote-tag resolution
        + split + media + retry/fallback). Shared by the solicited reply path
        and unsolicited (background-task) turns so both deliver identically.

        Every <quote> tag is stripped; each one attaches as a real Telegram
        reply on the first bubble of the text that follows it."""
        if not response and not thinking:
            return

        # Thinking: send as expandable blockquote after main response
        if thinking and self._state.thinking_on:
            truncated = thinking[:2000]
            if len(thinking) > 2000:
                truncated += f"\n... ({len(thinking)} chars total)"
            think_html = f"<blockquote expandable>\U0001f9e0 {gfm_to_tg_html(truncated)}</blockquote>"
            try:
                await bot.send_message(chat_id=chat_id, text=think_html, parse_mode="HTML")
            except Exception as e:
                logger.warning("thinking send failed: %s", e)

        if not response:
            return

        reply_ids: dict[int, int] = {}
        segments: list[tuple[int | None, str]] = []
        pos = 0
        quote_id: int | None = None
        for match in _QUOTE_TAG.finditer(response):
            segments.append((quote_id, response[pos : match.start()]))
            quote_id = self._resolve_quote_target(match.group(1).strip())
            pos = match.end()

        if segments:
            segments.append((quote_id, response[pos:]))
            bubbles: list[dict[str, str]] = []
            for seg_reply, seg_text in segments:
                seg_bubbles = split_for_tg_typed(seg_text.strip())
                if not seg_bubbles:
                    continue
                if seg_reply is not None:
                    reply_ids[len(bubbles)] = seg_reply
                bubbles.extend(seg_bubbles)
        else:
            bubbles = split_for_tg_typed(response)

        total = len(bubbles)
        for idx, bubble in enumerate(bubbles):
            reply_to_id = reply_ids.get(idx)
            if bubble["kind"] == "text":
                send_kwargs = dict(
                    chat_id=chat_id,
                    text=gfm_to_tg_html(bubble["text"]),
                    parse_mode="HTML",
                )
                fallback_kwargs = dict(chat_id=chat_id, text=bubble["text"])
                if reply_to_id is not None:
                    send_kwargs["reply_to_message_id"] = reply_to_id
                    fallback_kwargs["reply_to_message_id"] = reply_to_id
                ok = await self._send_text_bubble(bot, send_kwargs, fallback_kwargs)
                if not ok:
                    lost = total - idx
                    logger.warning(
                        "send_message failed at bubble %d/%d — %d bubble(s) of the turn stopped",
                        idx + 1, total, lost,
                    )
                    if self._alerts is not None:
                        try:
                            self._alerts.write(
                                "warn",
                                "tg_send_rejected",
                                f"send_message failed at bubble {idx + 1}/{total}; "
                                f"{lost} bubble(s) of the turn stopped",
                                source="loop.check_flush",
                                fingerprint="tg.send_rejected",
                            )
                        except Exception as ae:
                            logger.warning("alerts.write failed: %s", ae)
                    break
            else:
                ok = await send_media(
                    bot, chat_id, bubble["kind"], bubble["path"],
                    reply_to=reply_to_id,
                    send_retry_max=self._cfg.send_retry_max,
                    retry_after_cap_sec=self._cfg.retry_after_cap_sec,
                )
                if not ok:
                    logger.warning(
                        "send_media failed for bubble %d/%d (%s) — continuing",
                        idx + 1, total, bubble["kind"],
                    )
            await asyncio.sleep(_SEND_GAP_SEC)
        else:
            logger.info("reply delivered: %d bubble(s)", total)
