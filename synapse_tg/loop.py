"""Async Telegram message loop: inbound text → provider → split → reply."""

from __future__ import annotations

import asyncio
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
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Bot, Update
from telegram.ext import ContextTypes

from synapse_core import bridge_state_store
from synapse_core.commands import messages
from synapse_core.commands.registry import CommandContext, Registry
from synapse_core.debounce import InboundBuffer
from synapse_core.providers.cc import ClaudeCodeProvider, QUOTE_SYSTEM_PROMPT
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
from .split import split_for_tg_typed
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

# Streaming config
_STREAM_EDIT_INTERVAL = 1.0   # seconds between intermediate edits
_STREAM_EDIT_CHARS = 200      # or every N new chars, whichever comes first

# Strip media tags from streaming preview — handled after completion
_MEDIA_TAG_RE = re.compile(r'<(image|gif|video|file)\s+path="[^"]*"\s*/?>', re.IGNORECASE)

TG_MEDIA_SYSTEM_PROMPT = (
    "To send media (photo/gif/video/file), put a tag in your reply, one tag "
    'per file: <image path="/abs/p.jpg"> <gif path="/abs/a.gif"> '
    '<video path="/abs/v.mp4"> <file path="/abs/doc.pdf"> '
    "(file = pdf/txt/any other type). Tag position is bubble order. "
    "The bridge uploads it and delivers a real Telegram media message. "
    "The path must be a real existing local file - never fabricate.\n\n"
    "Stickers: pair messages with stickers naturally — "
    "search sticker_search by vibe/emotion, call sticker_pick(id), "
    "then send with the matching tag: "
    '<image path="..."/> for .png/.jpg/.webp, <gif path="..."/> for .gif. '
    "When user sends an image: caption '1' = save as sticker (code-routed), "
    "'0' = skip; no digit = if it looks sticker-worthy, just call sticker_ingest "
    "directly. Desc format: emotion/scene | image text | one-line visual (CN preferred)."
)


def _recv_to_queue(provider: ClaudeCodeProvider, q: "queue.Queue") -> None:
    """Background thread: drain provider.recv() into a queue. Sentinel = None."""
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
    ) -> None:
        self._cfg = cfg
        self._sessions = sessions
        self._record_session = record_session
        self._idle_loop = idle_loop
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
        self._session_start_ts: float = 0.0
        self._user_initiated_close = False

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
            self._provider = None
        self._state.usage_total = {}
        self._state.last_assistant_usage = {}

    def _forget_session(self) -> None:
        self._state.session_id = None
        self._buffer = InboundBuffer()
        if self._sessions is not None and self._pending_chat_id is not None:
            self._sessions.forget(str(self._pending_chat_id))

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
            close_provider=lambda: self._swap_provider(None, None),
            forget_session=self._forget_session,
            persist_state=self._persist_state,
            clear_default_model=self._cfg.default_model,
            commands_doc_path=Path(__file__).resolve().parents[1] / "COMMANDS.md",
            fetch_diary=self._make_fetch_diary(),
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
            system_prompts=[QUOTE_SYSTEM_PROMPT, TG_MEDIA_SYSTEM_PROMPT],
        )

    def ensure_provider(self) -> None:
        if self._provider is None or not self._provider.alive:
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
                self._provider.kill()
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
                            self._session_start_ts = time.monotonic()
                            self._persist_state()
                        elif not self._session_start_ts:
                            self._session_start_ts = time.monotonic()
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
            try:
                ev = await loop.run_in_executor(None, lambda: q.get(timeout=60))
            except queue.Empty:
                logger.warning("stream: queue timeout — treating as dead")
                raise ProviderDeadError("recv queue timeout")

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
                            self._session_start_ts = time.monotonic()
                            self._persist_state()
                        elif not self._session_start_ts:
                            self._session_start_ts = time.monotonic()
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
                            # Strip media tags for the streaming preview
                            preview_chunk = _MEDIA_TAG_RE.sub("", chunk)
                            if preview_chunk:
                                accumulated += preview_chunk
                                chars_since_edit += len(preview_chunk)

                                if stream_msg_id is None:
                                    # First text — send initial plain message, stop typing
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

                    elif bt == "thinking":
                        if block.get("thinking"):
                            thinking_chunks.append(block["thinking"])

            elif t_type == "result":
                break

        self._death_count = 0
        full_text = "\n\n".join(text_chunks)
        thinking = "\n".join(thinking_chunks)

        return full_text, thinking, stream_msg_id

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
        """Close current provider and spawn fresh with --resume."""
        if self._provider is not None:
            try:
                self._provider.close()
            except Exception:
                pass
            self._provider = None
        self._state.session_id = sid
        if model:
            self._state.model = model
        self._provider = self._make_provider()
        self._provider.spawn()
        self._state.usage_total = {}
        self._state.last_assistant_usage = {}
        logger.info("respawn_with_resume sid=%s model=%s", sid, model)

    def replay_user_text(self, text: str) -> None:
        """Enqueue text on the inbound buffer for the next flush cycle."""
        self._buffer.add(text)

    def get_status(self) -> dict:
        """Return current bridge status for /info display."""
        alive = self._provider is not None and self._provider.alive
        age = None
        if self._session_start_ts:
            age = time.monotonic() - self._session_start_ts
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

    def _track(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._pending_chat_id = chat_id

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or update.message.text is None:
            return
        text = update.message.text.strip()
        if not text:
            return
        self._track(context.bot, update.message.chat_id)

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

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message is None or not update.message.photo:
            return
        self._track(context.bot, update.message.chat_id)
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
        self._track(context.bot, update.message.chat_id)
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
        self._track(context.bot, update.message.chat_id)
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
        self._track(context.bot, update.message.chat_id)
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
        self._track(context.bot, update.message.chat_id)
        path = await materialize_video(context.bot, update.message, self._cfg.data_dir)
        if path:
            instruction = build_read_instruction([path])
            caption = (update.message.caption or "").strip()
            body = f"{caption}\n{instruction}" if caption else instruction
            self._buffer.add(body)
            logger.debug("buffered video: %s", path)

    async def check_flush(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._buffer.ready() or self._pending_chat_id is None:
            return
        bot = self._bot or context.bot
        chat_id = self._pending_chat_id
        body = self._buffer.flush()
        if not body:
            return

        logger.debug("flush: %r", body[:120])
        typing = TypingAction(bot, chat_id)

        async with self._lock:
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
            except ProviderDeadError as e:
                if self._user_initiated_close:
                    self._user_initiated_close = False
                    return
                logger.error("provider error: %s", e)
                self._respawn()
                await bot.send_message(chat_id=chat_id, text=messages.t("provider.restarting", self._state.voice_style))
                return
            except Exception as e:
                logger.error("unexpected error: %s", e)
                await bot.send_message(chat_id=chat_id, text=messages.t("bridge.error", self._state.voice_style))
                return
            finally:
                typing.stop()

        # Pre-send merge: new inbound arrived while cc was producing this reply.
        # Drop the stale reply, re-queue old body, let next flush merge.
        if self._buffer:
            if stream_msg_id is not None:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=stream_msg_id)
                except Exception:
                    pass
            merged = f"{_MERGE_NOTE}\n{body}" if body else ""
            self._buffer.prepend(merged)
            logger.info("pre-send merge: reply dropped, %d chars re-queued", len(body))
            return

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

        bubbles = split_for_tg_typed(response)
        text_bubbles = [b for b in bubbles if b["kind"] == "text"]

        # Single text bubble: edit preview in-place with HTML formatting.
        # Multiple bubbles: delete the preview and send all fresh — avoids
        # the jarring "full text shrinks to first paragraph" effect.
        skip_first_text = False
        if stream_msg_id is not None and text_bubbles:
            if len(text_bubbles) == 1:
                first = text_bubbles[0]["text"]
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=stream_msg_id,
                        text=gfm_to_tg_html(first), parse_mode="HTML",
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id, message_id=stream_msg_id, text=first,
                        )
                    except Exception:
                        pass
                skip_first_text = True
            else:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=stream_msg_id)
                except Exception:
                    pass

        for bubble in bubbles:
            if bubble["kind"] == "text":
                if skip_first_text:
                    skip_first_text = False
                    continue
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=gfm_to_tg_html(bubble["text"]),
                        parse_mode="HTML",
                    )
                except Exception:
                    await bot.send_message(chat_id=chat_id, text=bubble["text"])
            else:
                await send_media(bot, chat_id, bubble["kind"], bubble["path"])
            await asyncio.sleep(_SEND_GAP_SEC)
