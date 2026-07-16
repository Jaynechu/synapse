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
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from telegram import Bot, Update
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

from synapse_core import bridge_state_store
from synapse_core.marrow_session import get_session_created_at, get_session_effort, regen_suppress_path
from synapse_core.commands import messages
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.debounce import InboundBuffer
from synapse_core.providers.cc import ClaudeCodeProvider, MEDIA_SYSTEM_PROMPT, NIGHT_SYSTEM_PROMPT, QUOTE_SYSTEM_PROMPT
from synapse_core.providers.errors import ProviderDeadError
from synapse_core.state import BridgeState

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
from . import outbox
from .split import split_for_tg, split_for_tg_typed
from .typing_action import TypingAction

if TYPE_CHECKING:
    from .config import TgConfig

logger = logging.getLogger(__name__)

_MERGE_NOTE = (
    "[bridge: your previous reply was dropped — new messages arrived "
    "mid-turn. Answer the full merged message below.]"
)

_SEND_GAP_SEC = 0.05
_MAX_CONSECUTIVE_DEATHS = 3
_FLUSH_INTERVAL_SEC = 0.5
# Extra seconds added on top of a 429 RetryAfter before retrying the send.
_RETRY_AFTER_MARGIN_SEC = 0.5

# Streaming config
_STREAM_EDIT_INTERVAL = 1.0   # seconds between intermediate edits
_STREAM_EDIT_CHARS = 200      # or every N new chars, whichever comes first

# Strip media tags from streaming preview — handled after completion
_MEDIA_TAG_RE = re.compile(r'<(image|gif|video|file)\s+path="[^"]*"\s*/?>', re.IGNORECASE)


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


def _recv_to_queue(provider: ClaudeCodeProvider, q: "queue.Queue") -> None:
    """Background thread: drain provider.recv() into a queue. Sentinel = None.

    The provider owns liveness now (soft check + hard idle kill in recv), so a
    stall/death surfaces as an exception put on the queue — no per-turn queue
    timeout here. The async consumer blocks on q.get() until an event, the
    exception, or the None sentinel arrives.
    """
    try:
        for ev in provider.recv():
            q.put(ev)
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
        self._pending_chat_id: int | None = None
        self._bot: Bot | None = None
        self._state_path = cfg.data_dir / "bridge_state.json"
        self._state = self._load_state()
        self._registry = self._build_registry()
        self._queued_extra_bubbles: list[str] = []
        self._pending_user_id: int | None = None
        self._turn_user_id: int | None = None
        self._same_sender_interrupted = False
        self._session_created_at: str | None = None
        if self._state.session_id:
            self._session_created_at = get_session_created_at(
                cfg.session_created_command, self._state.session_id
            )
        self._user_initiated_close = False
        self._msg_id_cache: collections.OrderedDict[int, str] = collections.OrderedDict()

    def _load_state(self) -> BridgeState:
        state = BridgeState(model=self._cfg.default_model)
        saved = bridge_state_store.load(self._state_path)
        for k, v in saved.items():
            if hasattr(state, k):
                setattr(state, k, v)
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
        "from zoneinfo import ZoneInfo",
        "from marrow.timecue import parse_time_cue",
        "from marrow.daemon import recall",
        "_m=ZoneInfo('Australia/Melbourne')",
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
            clear_default_model=self._cfg.default_model,
            commands_doc_path=Path(__file__).resolve().parents[1] / "COMMANDS.md",
            fetch_diary=self._make_fetch_diary(),
            record_effort=self._record_effort,
            resolve_session_effort=lambda sid: get_session_effort(
                self._cfg.session_get_effort_command, sid
            ),
        )
        return Registry(ctx)

    def _make_provider(self) -> ClaudeCodeProvider:
        cfg = self._cfg
        state = self._state
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
                        chunks.append(block["text"])
                    elif bt == "thinking":
                        if block.get("thinking"):
                            thinking.append(block["thinking"])
            elif t == "result":
                break
        self._death_count = 0
        return "\n\n".join(chunks), "\n".join(thinking)

    async def _stream_response(
        self, bot: Bot, chat_id: int, typing: TypingAction
    ) -> tuple[str, str]:
        """Stream provider response live via edit_message_text.

        Returns (full_text, thinking) after completion.
        Typing action is stopped once the first message is sent.
        """
        assert self._provider is not None

        q: queue.Queue = queue.Queue()
        t = threading.Thread(
            target=_recv_to_queue,
            args=(self._provider, q),
            daemon=True,
        )
        t.start()

        text_chunks: list[str] = []
        thinking_chunks: list[str] = []
        stream_msg_id: int | None = None
        accumulated = ""        # preview text accumulated so far
        preview_frozen = False  # stop updating preview after first \n\n
        last_edit_time = 0.0
        chars_since_edit = 0

        async def _do_edit(text: str) -> None:
            nonlocal last_edit_time, chars_since_edit
            if stream_msg_id is None or not text:
                return
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=stream_msg_id,
                    text=text,
                )
                last_edit_time = time.monotonic()
                chars_since_edit = 0
            except Exception:
                # Unchanged text or rate-limit — skip silently
                pass

        loop = asyncio.get_event_loop()

        while True:
            # No queue timeout: the provider's own liveness logic (soft check +
            # hard idle kill) surfaces stall/death as an exception on the queue.
            ev = await loop.run_in_executor(None, q.get)

            if ev is None:
                # Sentinel: thread finished cleanly
                break
            if isinstance(ev, Exception):
                raise ev

            t_type = ev.get("type")

            if t_type == "system":
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

            if t_type == "assistant":
                msg = ev.get("message") or {}
                for block in msg.get("content", []):
                    bt = block.get("type")
                    if bt == "text":
                        chunk = block.get("text", "")
                        if chunk:
                            text_chunks.append(chunk)
                            if not preview_frozen:
                                preview_chunk = _MEDIA_TAG_RE.sub("", chunk)
                                if preview_chunk:
                                    accumulated += preview_chunk
                                    # Freeze preview at first bubble boundary
                                    if "\n\n" in accumulated:
                                        accumulated = accumulated.split("\n\n", 1)[0]
                                        preview_frozen = True
                                    chars_since_edit += len(preview_chunk)

                                    if not accumulated.strip():
                                        continue

                                    if stream_msg_id is None:
                                        typing.stop()
                                        sent = await bot.send_message(
                                            chat_id=chat_id, text=accumulated
                                        )
                                        stream_msg_id = sent.message_id
                                        last_edit_time = time.monotonic()
                                        chars_since_edit = 0
                                    else:
                                        now = time.monotonic()
                                        if (
                                            now - last_edit_time >= _STREAM_EDIT_INTERVAL
                                            or chars_since_edit >= _STREAM_EDIT_CHARS
                                        ):
                                            await _do_edit(accumulated)

                    elif bt == "tool_use":
                        if not typing.running:
                            typing.start()
                    elif bt == "thinking":
                        if block.get("thinking"):
                            thinking_chunks.append(block["thinking"])
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    self._merge_usage(usage)
                    snap = {k: v for k, v in usage.items() if isinstance(v, int)}
                    if snap:
                        self._state.last_assistant_usage = snap

            elif t_type == "result":
                usage = ev.get("usage")
                if isinstance(usage, dict):
                    self._merge_usage(usage)
                break

        self._death_count = 0
        full_text = "\n\n".join(text_chunks)
        thinking = "\n".join(thinking_chunks)

        return full_text, thinking, stream_msg_id

    def _merge_usage(self, usage: dict[str, Any]) -> None:
        for k, v in usage.items():
            if isinstance(v, int):
                self._state.usage_total[k] = self._state.usage_total.get(k, 0) + v

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

    def _outbox_db(self) -> str:
        if not self._cfg.marrow_db:
            return ""
        return str(Path(self._cfg.marrow_db).expanduser())

    def sweep_outbox_orphans(self) -> None:
        """Startup: fail any stale 'claimed' tg row (crash orphan), never resend."""
        if self._cfg.chat_id is None:
            return
        db = self._outbox_db()
        if not db:
            return
        for row_id in outbox.sweep_orphan_claimed(db):
            logger.warning("outbox orphan claimed row #%d -> failed (not resent)", row_id)
            if self._alerts is not None:
                self._alerts.write(
                    "warn", "tg_outbox_orphan",
                    f"outbox row #{row_id} was claimed at crash — failed, not resent",
                    source="synapse-tg",
                    fingerprint="tg.outbox_orphan",
                )

    async def outbox_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Deliver pending outbox notes targeted at tg. Runs on the job queue."""
        if self._cfg.chat_id is None:
            return
        db = self._outbox_db()
        if not db:
            return
        rows = outbox.claim_pending(db)
        if not rows:
            return
        bot = self._bot or context.bot
        chat_id = self._cfg.chat_id
        for row in rows:
            await self._deliver_outbox_row(bot, chat_id, row["id"], row["body"] or "")

    async def _deliver_outbox_row(
        self, bot: Bot, chat_id: int, row_id: int, body: str
    ) -> None:
        db = self._outbox_db()
        bubbles = split_for_tg(body) or [body]
        attempts = 0
        for bubble in bubbles:
            sent = False
            for attempt in range(self._cfg.outbox_retry_max):
                attempts += 1
                try:
                    await bot.send_message(chat_id=chat_id, text=bubble)
                    sent = True
                    break
                except Exception as e:
                    logger.warning(
                        "outbox row #%d send failed (attempt %d/%d): %s",
                        row_id, attempt + 1, self._cfg.outbox_retry_max, e,
                    )
            if not sent:
                outbox.mark_failed(db, row_id, retry_count=attempts)
                logger.error("outbox row #%d -> failed after retries", row_id)
                if self._alerts is not None:
                    self._alerts.write(
                        "warn", "tg_outbox_failed",
                        f"outbox row #{row_id} send failed after {attempts} attempts",
                        source="synapse-tg",
                        fingerprint="tg.outbox_failed",
                    )
                return
            await asyncio.sleep(_SEND_GAP_SEC)
        outbox.mark_sent(db, row_id)

    def _track(self, bot: Bot, chat_id: int, user_id: int | None = None) -> None:
        self._bot = bot
        self._pending_chat_id = chat_id
        if user_id is not None:
            self._pending_user_id = user_id
            if self._turn_user_id is not None and user_id == self._turn_user_id:
                self._same_sender_interrupted = True

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.message.text is None:
            return
        text = update.message.text.strip()
        if not text:
            return
        uid = update.message.from_user.id if update.message.from_user else None
        self._track(context.bot, update.message.chat_id, uid)

        action, ack = self._registry.dispatch(text)
        inject = self._registry.pending_rewrite
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
        logger.debug("buffered text: %r (len=%d)", text[:80], len(self._buffer))
        if update.message:
            self._msg_id_cache[update.message.message_id] = text
            if len(self._msg_id_cache) > 50:
                self._msg_id_cache.popitem(last=False)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.photo:
            return
        uid = update.message.from_user.id if update.message.from_user else None
        self._track(context.bot, update.message.chat_id, uid)
        paths = await materialize_photo(context.bot, update.message, self._cfg.data_dir)
        if paths:
            instruction = build_read_instruction(paths)
            caption = (update.message.caption or "").strip()
            body = f"{caption}\n{instruction}" if caption else instruction
            self._buffer.add(body)
            logger.debug("buffered photo: %s", paths)

    async def on_animation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.animation:
            return
        uid = update.message.from_user.id if update.message.from_user else None
        self._track(context.bot, update.message.chat_id, uid)
        path = await materialize_animation(context.bot, update.message, self._cfg.data_dir)
        if path:
            instruction = build_read_instruction([path])
            caption = (update.message.caption or "").strip()
            body = f"{caption}\n{instruction}" if caption else instruction
            self._buffer.add(body)
            logger.debug("buffered animation: %s", path)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.document:
            return
        uid = update.message.from_user.id if update.message.from_user else None
        self._track(context.bot, update.message.chat_id, uid)
        path = await materialize_document(context.bot, update.message, self._cfg.data_dir)
        if path:
            instruction = build_read_instruction([path])
            caption = (update.message.caption or "").strip()
            body = f"{caption}\n{instruction}" if caption else instruction
            self._buffer.add(body)
            logger.debug("buffered document: %s", path)

    async def on_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.sticker:
            return
        uid = update.message.from_user.id if update.message.from_user else None
        self._track(context.bot, update.message.chat_id, uid)
        path = await materialize_sticker(context.bot, update.message, self._cfg.data_dir)
        if path:
            stk = update.message.sticker
            meta = f"[sticker: emoji={stk.emoji or '?'}, set={stk.set_name or 'none'}]"
            instruction = build_read_instruction([path])
            self._buffer.add(f"{meta}\n{instruction}")
            logger.debug("buffered sticker: %s", path)

    async def on_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.video:
            return
        uid = update.message.from_user.id if update.message.from_user else None
        self._track(context.bot, update.message.chat_id, uid)
        path = await materialize_video(context.bot, update.message, self._cfg.data_dir)
        if path:
            instruction = build_read_instruction([path])
            caption = (update.message.caption or "").strip()
            body = f"{caption}\n{instruction}" if caption else instruction
            self._buffer.add(body)
            logger.debug("buffered video: %s", path)

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
        self._turn_user_id = self._pending_user_id
        self._same_sender_interrupted = False
        body = self._buffer.flush()
        if not body:
            self._turn_user_id = None
            return

        logger.debug("flush: %r", body[:120])
        typing = TypingAction(bot, chat_id)

        async with self._lock:
            try:
                # Retry-once: on a mid-turn stall/death, respawn resuming the
                # same sid and re-send the SAME body ONCE. Second failure ->
                # user-facing notice. Bridges emit outbound only from completed
                # events, so a retried turn double-sends nothing; any partially
                # flushed stream bubble is abandoned (stream_msg_id reset below).
                response = thinking = None
                stream_msg_id = None
                for attempt in range(2):
                    try:
                        self.ensure_provider()
                        assert self._provider is not None
                        typing.start()
                        await asyncio.to_thread(self._provider.send, body)
                        response, thinking, stream_msg_id = await self._stream_response(bot, chat_id, typing)
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
                            # Abandon any partial stream bubble; retry cleanly.
                            stream_msg_id = None
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

        # Pre-send merge: only if the SAME sender sent new messages while thinking.
        # Other users' messages (group chat) never interrupt the current reply.
        if self._same_sender_interrupted:
            if stream_msg_id is None:
                # Still thinking — no bubble visible. Safe to merge.
                merged = f"{_MERGE_NOTE}\n{body}" if body else ""
                self._buffer.prepend(merged)
                logger.info("pre-send merge (thinking): reply dropped, %d chars re-queued", len(body))
                self._turn_user_id = None
                return
            # Bubble already visible — deliver normally. New messages queued for next flush.
            logger.info("same sender new inbound during streaming — delivering reply, new messages queued")
        self._turn_user_id = None

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
            except Exception:
                pass

        if not response:
            return

        reply_to_id = None
        quote_match = re.search(r"<quote>(.*?)</quote>", response, re.DOTALL)
        if quote_match:
            fragment = quote_match.group(1).strip()
            response = (response[: quote_match.start()] + response[quote_match.end() :]).strip()
            for msg_id, msg_text in reversed(self._msg_id_cache.items()):
                if fragment.lower() in msg_text.lower():
                    reply_to_id = msg_id
                    break
            if stream_msg_id is not None and reply_to_id is not None:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=stream_msg_id)
                except Exception:
                    pass
                stream_msg_id = None

        bubbles = split_for_tg_typed(response)
        text_bubbles = [b for b in bubbles if b["kind"] == "text"]

        # Edit streaming preview in-place to first bubble, then append the rest.
        # If the final handoff edit fails entirely, fall through and send bubble 0
        # as a fresh message instead of silently dropping it.
        skip_first_text = False
        if stream_msg_id is not None and text_bubbles:
            first = text_bubbles[0]["text"]
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=stream_msg_id,
                    text=gfm_to_tg_html(first), parse_mode="HTML",
                )
                skip_first_text = True
            except Exception as html_exc:
                if "message is not modified" in str(html_exc).lower():
                    # Preview already matches bubble 0 — already delivered, not a failure.
                    skip_first_text = True
                else:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id, message_id=stream_msg_id, text=first,
                        )
                        skip_first_text = True
                    except Exception as plain_exc:
                        if "message is not modified" in str(plain_exc).lower():
                            skip_first_text = True
                        else:
                            logger.warning(
                                "stream final edit failed — re-sending bubble 0 as new message"
                            )

        total = len(bubbles)
        for idx, bubble in enumerate(bubbles):
            if bubble["kind"] == "text":
                if skip_first_text:
                    skip_first_text = False
                    continue
                send_kwargs = dict(
                    chat_id=chat_id,
                    text=gfm_to_tg_html(bubble["text"]),
                    parse_mode="HTML",
                )
                fallback_kwargs = dict(chat_id=chat_id, text=bubble["text"])
                if reply_to_id is not None:
                    send_kwargs["reply_to_message_id"] = reply_to_id
                    fallback_kwargs["reply_to_message_id"] = reply_to_id
                    reply_to_id = None
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
                if reply_to_id is not None:
                    reply_to_id = None
            await asyncio.sleep(_SEND_GAP_SEC)
