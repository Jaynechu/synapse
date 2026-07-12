"""Bridge main loop: glue between iLink (A3) and the Claude Code provider (A2)."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from synapse_core import last_active
from synapse_core.marrow_session import get_session_created_at, regen_suppress_path
from synapse_core.alerts import AlertSink
from synapse_core.anchor import quote_prefix, time_anchor
from synapse_core.commands import messages
from synapse_core.commands.registry import Registry
from .config import Config
from synapse_core.debounce import InboundBuffer
from .media.inbound import build_read_tool_instruction, materialize
from .media.outbound import dispatch_media_bubble
from synapse_core.providers.base import Provider
from synapse_core.providers.errors import ProviderDeadError
from synapse_core.sessionend.idle import IdleFireLoop
from synapse_core.sessionend.tracker import SessionTracker
from .split import format_thinking_bubbles, split_for_wechat_typed
from synapse_core.state import BridgeState
from .typing_ping import TypingPing

logger = logging.getLogger(__name__)

DEFAULT_ALERT_DIR = Path.home() / ".config" / "synapse-wx" / "alerts"
DEFAULT_MEDIA_DIR = Path.home() / ".config" / "synapse-wx" / "media"
_DEFAULT_BUBBLE_GAP_SEC = 0.8
# Quote-lite (post-v4): cc emits a <quote>FRAGMENT</quote> block ANYWHERE in
# the reply. The real ref_msg outbound path was attempted live but WeChat does
# NOT render the bubble as a quote-reply (see MAP.md "Known limitations").
# Workaround: extract FRAGMENT before bubble splitting (so a multi-line tag
# never leaks across bubbles as literal text), strip the tag, and prepend a
# standalone visual fake-quote bubble (▎FRAGMENT, truncated) to the reply.
_QUOTE_TAG = re.compile(
    r"<quote>(.*?)</quote>\n?", re.DOTALL | re.IGNORECASE
)
_FAKE_QUOTE_PREFIX = "▎"
# Pre-send merge: prepended to the re-queued old body so cc knows its prior
# answer never reached the user and it should answer the merged thread fresh.
_MERGE_NOTE = (
    "[bridge: your previous reply was dropped — new messages arrived "
    "mid-turn. Answer the full merged message below.]"
)
# Truncate display fragment: 40 CN chars or 80 ASCII chars.
_FAKE_QUOTE_CN_MAX = 40
_FAKE_QUOTE_ASCII_MAX = 80


_STICKER_CAPTION_RE = re.compile(r"^1(?:\s+(.+))?$", re.DOTALL)


def _parse_sticker_caption(
    body: str, media_events: list[dict]
) -> tuple[str, str] | None:
    """Detect sticker caption when images present. Returns (action, desc) or None."""
    if not any(e.get("type") == "image" for e in media_events):
        return None
    lines = [ln for ln in body.split("\n") if ln.strip() != "."]
    text = "\n".join(lines).strip()
    if not text:
        return None
    if text == "0":
        return ("suppress", "")
    m = _STICKER_CAPTION_RE.match(text)
    if m:
        return ("save", (m.group(1) or "").strip())
    return None


def _build_fake_quote_bubble(frag: str) -> str:
    """Format ``▎FRAGMENT`` with display truncation.

    Limit is 40 chars if the fragment contains any CJK code-point, else 80
    ASCII chars. Truncated output ends with ``…``. Newlines inside the
    fragment are collapsed to a single space so the bubble stays one visual
    line (matching WeChat's native quote preview).
    """
    cleaned = " ".join(frag.split())
    has_cjk = any("一" <= ch <= "鿿" for ch in cleaned)
    limit = _FAKE_QUOTE_CN_MAX if has_cjk else _FAKE_QUOTE_ASCII_MAX
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return f"{_FAKE_QUOTE_PREFIX}{cleaned}"


class MainLoop:
    """Drives the inbound poll → debounce → provider → outbound split cycle."""

    def __init__(
        self,
        *,
        ilink: Any,  # ILinkClient duck-typed for testability
        provider_factory: Callable[..., Provider],
        state: BridgeState,
        sessions: SessionTracker,
        idle_loop: IdleFireLoop | None = None,
        buffer: InboundBuffer | None = None,
        poll_interval_sec: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], datetime] = datetime.now,
        sleeper: Callable[[float], None] = time.sleep,
        alert_dir: Path = DEFAULT_ALERT_DIR,
        registry: Registry | None = None,
        alerts: AlertSink | None = None,
        cfg: Config | None = None,
        record_session: Callable[[str, str | None], None] | None = None,
        channel: str,
        last_active_path: Path,
        channel_label: str,
        media_dir: Path = DEFAULT_MEDIA_DIR,
    ) -> None:
        self._ilink = ilink
        self._provider_factory = provider_factory
        self.state = state
        self._sessions = sessions
        self._idle_loop = idle_loop
        self._buffer = buffer if buffer is not None else InboundBuffer(clock=clock)
        self._poll_interval_sec = poll_interval_sec
        self._clock = clock
        self._wallclock = wallclock
        self._sleeper = sleeper
        self._alert_dir = Path(alert_dir)
        self._registry = registry
        self._alerts = alerts
        self._cfg = cfg
        # Config-first bubble pacing; falls back to default when cfg absent.
        self._bubble_gap_sec = (
            cfg.bubble_gap_sec if cfg is not None else _DEFAULT_BUBBLE_GAP_SEC
        )
        # B1: best-effort sessions row writer. Default no-op so tests + mock
        # provider paths don't pay the marrow-CLI penalty.
        self._record_session = record_session or (lambda _sid, _model: None)
        # B6: per-turn last_active.json channel tag.
        self._channel = channel
        self._last_active_path = Path(last_active_path)
        self._channel_label = channel_label

        self._provider: Provider | None = None
        self._last_from_wxid: str | None = None
        self._last_ctx_token: str = ""
        self._paused = False
        # C0: accumulate per-turn inbound media events (image/voice/pdf/video).
        # Flushed alongside the text buffer; each event becomes a local Path
        # and a Read-tool instruction appended to the assembled prompt.
        self._pending_media: list[dict] = []
        self._media_dir = Path(media_dir)
        # B10 /status: monotonic start ts + last successful iLink poll ts.
        self._start_ts: float = self._clock()
        # /info reports cc-session age, not bridge age. Stamped fresh in
        # _drain_recv on every cc `system{init}` (new sid) and on boot_resume.
        # forget_session clears it so the next live sid restamps cleanly.
        self._session_created_at: str | None = None
        if self.state.session_id and cfg is not None:
            self._session_created_at = get_session_created_at(
                cfg.session_created_command, self.state.session_id
            )
        self._last_poll_ok_ts: float = 0.0
        # Restart self-announce: fired once after the first successful poll
        # so the message is never dropped while iLink is still warming up.
        self._announce_pending: bool = False
        self._announce_target: str = ""
        self._announce_text: str = ""
        # E-polish /thinking: captured between _drain_recv → maybe_flush so we
        # can emit a single 【思考】 bubble per turn when state.thinking_on.
        self._last_thinking: str = ""
        # Lazy typing: started in maybe_flush() right before provider.send —
        # i.e. only when cc actually starts thinking, not during the debounce
        # buffer wait. Stopped after first reply bubble lands (or on any
        # error path).
        self._typing_ping: TypingPing | None = None
        # B7: consecutive provider-death counter (reset on successful recv).
        # >=3 with session_id set → critical alert + user bubble.
        self._provider_death_count: int = 0

        # Decouple inbound long-poll from outbound flush:
        # - _poll_thread runs tick() (blocks on long poll, may sit ~20s)
        # - _flush_thread runs maybe_flush() on a tight 1s cadence
        # `_state_lock` protects shared state written by tick / read by flush:
        # _buffer, _last_from_wxid, _last_ctx_token, _pending_media,
        # state.last_user_msg_ts. Held ONLY during data handoff — never
        # across provider.send / _drain_recv (would re-serialize the loops).
        self._state_lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._poll_thread: threading.Thread | None = None
        self._flush_thread: threading.Thread | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    def start(self, *, boot_resume_sid: str | None = None) -> None:
        """Boot the loop. When `boot_resume_sid` is set, the first cc spawn is
        a --resume of that sid (used after bridge restart to keep continuity).
        Bare start() falls back to a fresh cc, matching the legacy behavior.
        """
        if self._flush_thread and self._flush_thread.is_alive():
            return
        if boot_resume_sid:
            try:
                self._provider = self._provider_factory(resume_sid=boot_resume_sid)
            except TypeError:
                # factory may be a no-kwargs stub in tests
                self._provider = self._provider_factory()
        else:
            self._provider = self._provider_factory()
        self._provider.spawn()
        # Seed state.session_id so MainLoop.tick treats the resumed cc as the
        # current session without waiting for cc's `system{init}` echo.
        if boot_resume_sid and not self.state.session_id:
            self.state.session_id = boot_resume_sid
            cfg = self._cfg
            sid = boot_resume_sid
            self._session_created_at = (
                get_session_created_at(cfg.session_created_command, sid)
                if cfg is not None
                else None
            ) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._stop_evt.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_run, name="synapse-wx-poll-loop", daemon=True
        )
        self._flush_thread = threading.Thread(
            target=self._flush_run, name="synapse-wx-flush-loop", daemon=True
        )
        self._poll_thread.start()
        self._flush_thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        # Flush thread cycles fast (1s); poll thread may be parked in a
        # long-poll HTTP call — give it the full HTTP timeout to wake.
        if self._flush_thread:
            self._flush_thread.join(timeout=5.0)
            self._flush_thread = None
        if self._poll_thread:
            self._poll_thread.join(timeout=25.0)
            self._poll_thread = None
        if self._provider is not None:
            try:
                self._provider.close()
            except Exception as e:
                logger.warning("provider close error: %s", e)
            self._provider = None

    def join(self, timeout: float | None = None) -> None:
        # External callers join the flush thread — it owns the outbound
        # path and provider lifecycle, so its exit is the true "loop done".
        if self._flush_thread:
            self._flush_thread.join(timeout=timeout)

    def _poll_run(self) -> None:
        while not self._stop_evt.is_set():
            if not self._paused:
                try:
                    self.tick()
                except Exception as e:
                    logger.warning("poll tick error: %s", e, exc_info=True)
            self._sleeper(self._poll_interval_sec)

    # Flush thread cadence: much tighter than poll_interval_sec because the
    # flush loop is a cheap buffer.ready() check, but its sleep directly adds
    # to "user → typing" latency. 0.2s caps that overhead at ~100ms avg.
    _FLUSH_TICK_SEC: float = 0.2

    def _flush_run(self) -> None:
        while not self._stop_evt.is_set():
            if not self._paused:
                try:
                    self.maybe_flush()
                except Exception as e:
                    logger.warning("flush tick error: %s", e, exc_info=True)
            self._sleeper(self._FLUSH_TICK_SEC)

    def pause_poll(self) -> None:
        """Halt tick()/maybe_flush() until resume_poll. Provider stays alive."""
        self._paused = True

    def resume_poll(self) -> None:
        """Resume normal polling after a pause."""
        self._paused = False

    def _provider_alive(self) -> bool:
        """True if the underlying provider subprocess is alive."""
        if self._provider is None:
            return False
        try:
            return self._provider.is_alive()
        except Exception as e:
            logger.warning("provider is_alive raised: %s", e)
            return False

    def _ensure_provider(self) -> bool:
        """B11: ensure a live provider, lazy-respawning on idle-close.

        Returns True if a live provider is available for this turn. If the
        provider is None or dead AND state.session_id is set, spawn a fresh
        one via factory(model=state.model, resume_sid=state.session_id) — the
        idle-fire path keeps the sid so the conversation continues here.
        Returns False if no provider is available and no sid to resume.
        """
        if self._provider_alive():
            return True
        sid = self.state.session_id
        if not sid:
            return False
        try:
            new_provider = self._provider_factory(
                model=self.state.model, resume_sid=sid
            )
        except TypeError:
            new_provider = self._provider_factory()
        try:
            try:
                new_provider.spawn(env={})
            except TypeError:
                # mock factories may produce already-spawned providers
                new_provider.spawn()
        except Exception as e:
            logger.error("_ensure_provider spawn failed: %s", e)
            self._provider_death_count += 1
            self._handle_provider_dead(e, self._last_from_wxid, self._last_ctx_token)
            return False
        self._provider = new_provider
        return self._provider_alive()

    def idle_close_provider(self, sid: str) -> None:
        """B11: invoked by IdleFireLoop pre_spawn_hook on 6h idle fire.

        Close the live provider IFF the loop's current sid matches. Closing
        triggers cc-side SessionEnd → archive_events + bridge_owns marker.
        state.session_id is intentionally NOT cleared.
        """
        if self._provider is None:
            return
        if sid and self.state.session_id and sid != self.state.session_id:
            # Different sid (e.g. tracker has stale entry); leave live provider.
            return
        try:
            self._provider.close()
        except Exception as e:
            logger.warning("idle provider close failed: %s", e)
        self._provider = None

    # ── inbound ────────────────────────────────────────────────────

    def arm_restart_announce(self, target_wxid: str, text: str) -> None:
        """Queue a one-shot bubble fired after the first successful inbound poll.

        Beats `Timer(5.0)`: if iLink is still warming up at the 5s mark the
        send would silently drop. Holding it until poll-ok guarantees delivery.
        """
        if not target_wxid or not text:
            return
        self._announce_pending = True
        self._announce_target = target_wxid
        self._announce_text = text

    def tick(self) -> None:
        """One inbound poll: route bridge commands; buffer the rest for the provider."""
        try:
            msgs = self._ilink.poll_messages()
        except Exception as e:
            logger.warning("ilink poll failed: %s", e)
            return
        # B10: track last successful poll for /status iLink-ok check.
        self._last_poll_ok_ts = self._clock()
        if self._announce_pending:
            target = self._announce_target
            text = self._announce_text
            self._announce_pending = False
            self._announce_target = ""
            self._announce_text = ""
            try:
                self._ilink.send_text(target, "", text)
                logger.info("restart self-announce sent to %s", target)
            except Exception as e:
                logger.warning("restart self-announce failed: %s", e)
        for msg in msgs or []:
            from_wxid = msg.get("from_wxid") or msg.get("from_user_id") or ""
            ctx_token = msg.get("context_token") or ""
            # Publish from/ctx eagerly so a cmd-reply or the next flush can
            # use them even if this msg is consumed by the registry below.
            with self._state_lock:
                if from_wxid:
                    self._last_from_wxid = from_wxid
                if ctx_token:
                    self._last_ctx_token = ctx_token
            try:
                text = self._ilink.extract_text(msg)
            except Exception as e:
                logger.warning("extract_text failed: %s", e)
                text = ""
            # C0: surface media events alongside text so a pure-media bubble
            # (e.g. just a photo, no caption) still triggers a turn.
            media_events: list[dict] = []
            extract_media = getattr(self._ilink, "extract_media", None)
            if extract_media is not None:
                try:
                    media_events = list(extract_media(msg) or [])
                except Exception as e:
                    logger.warning("extract_media failed: %s", e)
                    media_events = []
            if not text and not media_events:
                continue
            # E-polish quote (inbound): iLink may carry a `reference` field on
            # a quoted reply. Prepend '[quoting: "..."]' so cc sees what the
            # user is replying to. Falls through harmlessly when absent.
            ref_text = self._extract_reference_text(msg)
            if ref_text:
                qp = quote_prefix(ref_text)
                if qp and text:
                    text = f"{qp}\n{text}"
                elif qp:
                    text = qp
            if text and self._registry is not None:
                verdict, reply = self._registry.dispatch(text)
                inject = self._registry.pending_rewrite
                if verdict == "handled":
                    if reply and from_wxid:
                        # Send the whole reply as one bubble — modern iLink
                        # preserves embedded `\n` in a single text_item and
                        # WeChat renders it as soft line breaks. Multi-line
                        # acks (/info /help /resume picker) stay one bubble.
                        try:
                            self._ilink.send_text(from_wxid, ctx_token, reply)
                        except Exception as e:
                            logger.warning(
                                "send_text (cmd reply) failed: %s", e
                            )
                    if inject:
                        with self._state_lock:
                            self._buffer.add(inject)
                            self.state.last_user_msg_ts = self._wallclock().timestamp()
                    continue
            # Commit buffer / media / last_user_msg_ts in one critical
            # section so the flush thread sees a consistent snapshot.
            with self._state_lock:
                if text:
                    self._buffer.add(text)
                if media_events:
                    self._pending_media.extend(media_events)
                    # Buffer guard: when text is empty but media is present, push a
                    # zero-width marker so InboundBuffer.ready() flips after the
                    # quiet window. Buffer skips empty/whitespace, so use a single
                    # space sentinel that splits cleanly downstream.
                    if not text:
                        self._buffer.add(".")
                self.state.last_user_msg_ts = self._wallclock().timestamp()

    def maybe_flush(self) -> None:
        """If quiet window elapsed, assemble anchor+buffer and run one provider turn."""
        # Cheap read of buffer state — hold lock only across the check.
        with self._state_lock:
            if not self._buffer.ready():
                return
        # B11: provider may have been closed by IdleFireLoop on 6h idle while
        # state.session_id was kept. Lazy-respawn with --resume <sid> so the
        # conversation continues on the next inbound. Out of lock: factory
        # may spawn a subprocess (slow).
        if not self._ensure_provider():
            return
        # Atomic snapshot: drain buffer + media events + recipient under lock.
        # Tick thread may be writing concurrently; we want one consistent turn.
        with self._state_lock:
            anchor_ts = self.state.last_user_msg_ts
            body = self._buffer.flush()
            media_events = list(self._pending_media)
            self._pending_media = []
            from_wxid = self._last_from_wxid
            ctx_token = self._last_ctx_token
        anchor = time_anchor(self._wallclock(), anchor_ts)
        # C2: sticker caption routing — intercept image+caption before materialize.
        sticker_cap = _parse_sticker_caption(body, media_events)
        if sticker_cap is not None:
            action, desc = sticker_cap
            n_images = sum(1 for e in media_events if e.get("type") == "image")
            if action == "suppress":
                media_events = [e for e in media_events if e.get("type") != "image"]
                if not media_events:
                    logger.info("sticker caption '0': suppressed %d image(s)", n_images)
                    return
            elif action == "save":
                label = "these images" if n_images > 1 else "this image"
                if desc:
                    body = (
                        f"[sticker-save] Save {label} as sticker via "
                        f"sticker_admin(action='ingest')."
                        f" Desc: {desc}"
                    )
                else:
                    body = (
                        f"[sticker-save] Save {label} as sticker via "
                        f"sticker_admin(action='ingest')."
                        f" Use vision to write desc."
                    )
                logger.info(
                    "sticker caption '1': routing %d image(s) for save", n_images
                )
        # C0: materialize any pending media → append Read-tool instruction.
        # Materialize is network IO; do it out of lock.
        media_paths = self._materialize_media(media_events)
        assembled = f"{anchor}\n{body}".rstrip()
        if media_paths:
            instruction = build_read_tool_instruction(media_paths)
            assembled = f"{assembled}\n\n{instruction}" if assembled else instruction

        try:
            # Lazy typing: fire indicator at the moment cc actually starts
            # thinking — NOT during the debounce buffer wait. Showing
            # "正在输入中" while the bridge is silently buffering would be
            # misleading: cc isn't working yet.
            if from_wxid and self._typing_ping is None:
                self._typing_ping = TypingPing(
                    self._ilink, from_wxid, ctx_token, interval=5.0
                )
                self._typing_ping.start()
            self._provider.send(assembled)
            reply_text = self._drain_recv()
        except ProviderDeadError as e:
            self._stop_typing()
            self._handle_provider_dead(e, from_wxid, ctx_token)
            return

        # B6: stamp the cross-channel last-active pointer once we have a sid.
        # Best-effort; never blocks the outbound path.
        sid = self.state.session_id
        if sid:
            try:
                last_active.write(
                    self._last_active_path,
                    sid,
                    self._channel,
                    self._wallclock().timestamp(),
                )
            except Exception as e:
                logger.warning("last_active write failed: %s", e)

        if not reply_text or not from_wxid:
            self._stop_typing()
            return
        # Pre-send merge: new inbound arrived while cc was producing this
        # reply → the reply answers a stale snapshot. Drop it, re-insert the
        # old body at the FRONT of the buffer (media already materialized —
        # its Read instruction rides along as text), and let the next flush
        # run one merged turn. No mid-stream abort needed (dodges cc #41665).
        # TypingPing is deliberately kept alive across the re-run.
        with self._state_lock:
            if self._buffer:
                replay = body
                if media_paths:
                    instruction = build_read_tool_instruction(media_paths)
                    replay = f"{replay}\n\n{instruction}" if replay else instruction
                merged = f"{_MERGE_NOTE}\n{replay}" if replay else ""
                self._buffer.prepend(merged)
                self._last_thinking = ""
                logger.info(
                    "pre-send merge: reply dropped, %d chars re-queued", len(replay)
                )
                return
        # Quote-lite: extract <quote>FRAGMENT</quote> from the WHOLE reply
        # BEFORE splitting on newlines. Pre-split extraction guarantees a
        # multi-line tag never leaks across bubbles as literal text. The
        # FRAGMENT becomes a standalone visual fake-quote bubble prepended
        # to the reply (▎FRAGMENT, truncated). The real ref_msg outbound
        # path was removed — WeChat does NOT render it.
        reply_text, fake_quote_bubbles = self._extract_quote_from_reply(reply_text)
        bubbles: list[dict] = split_for_wechat_typed(reply_text)
        # Tag stripping is unconditional (above); only the decorative bubbles
        # are gated behind /quote on so Lumi's default-off feed stays clean.
        if fake_quote_bubbles and self.state.quote_on:
            fqb = [{"kind": "text", "text": b} for b in fake_quote_bubbles]
            bubbles = fqb + bubbles
        # /thinking: prepend full thinking content as one or more 【思考】 /
        # ⋯ bubbles when enabled. Thinking head goes BEFORE the fake-quote
        # bubble so the visual flow stays thinking → quoted → reply.
        if self.state.thinking_on and self._last_thinking:
            tbs = format_thinking_bubbles(self._last_thinking)
            if tbs:
                bubbles = [{"kind": "text", "text": s} for s in tbs] + bubbles
        # Reset per-turn so a stale thinking buffer never leaks into the next.
        self._last_thinking = ""
        total = len(bubbles)
        for i, bubble in enumerate(bubbles):
            try:
                if bubble.get("kind") == "text":
                    sent = self._ilink.send_text(from_wxid, ctx_token, bubble["text"])
                    if not sent:
                        lost = total - i
                        logger.warning(
                            "send_text rejected bubble %d/%d — %d bubble(s) lost",
                            i + 1,
                            total,
                            lost,
                        )
                        if self._alerts is not None:
                            try:
                                self._alerts.write(
                                    "error",
                                    "wx_send_rejected",
                                    f"send_text rejected at bubble {i + 1}/{total}; "
                                    f"{lost} bubble(s) of the turn lost",
                                    source="loop.maybe_flush",
                                    fingerprint="wx.send_rejected",
                                )
                            except Exception as ae:
                                logger.warning("alerts.write failed: %s", ae)
                        if i == 0:
                            self._stop_typing()
                        break
                else:
                    ok = dispatch_media_bubble(
                        self._ilink,
                        bubble,
                        to_user_id=from_wxid,
                        context_token=ctx_token,
                        style=self.state.voice_style,
                        channel_label=self._channel_label,
                    )
                    if not ok:
                        logger.warning(
                            "dispatch_media_bubble returned False for %r", bubble
                        )
                        if i == 0:
                            self._stop_typing()
                        if i < total - 1:
                            self._sleeper(self._bubble_gap_sec)
                        continue
            except Exception as e:
                self._stop_typing()
                logger.warning("send bubble failed: %s", e)
                break
            if i == 0:
                self._stop_typing()
            if i < total - 1:
                self._sleeper(self._bubble_gap_sec)

    def _extract_quote_from_reply(
        self, reply_text: str
    ) -> tuple[str, list[str]]:
        """Pre-split <quote>FRAGMENT</quote> extraction over the WHOLE reply.

        Runs BEFORE bubble splitting so a multi-line quote block doesn't get
        sliced across bubbles. Returns (stripped_text, fake_quote_bubbles).
        Every well-formed tag becomes a ``▎FRAGMENT`` bubble.
        Empty FRAGMENT → tag stripped, no bubble.
        """
        matches = list(_QUOTE_TAG.finditer(reply_text))
        if not matches:
            return reply_text, []
        stripped = _QUOTE_TAG.sub("", reply_text)
        bubbles = []
        for m in matches:
            frag = m.group(1).strip()
            if frag:
                bubbles.append(_build_fake_quote_bubble(frag))
        return stripped, bubbles

    @staticmethod
    def _extract_reference_text(msg: dict) -> str:
        """Pull a human-readable quoted text out of the iLink `reference` field.

        iLink encodes the quoted message in a few shapes; we accept the most
        common: top-level ``reference``: ``{"text": "..."}`` or
        ``{"item_list": [{"type": 1, "text_item": {"text": "..."}}]}``. Empty
        string when no reference is present.
        """
        ref = msg.get("reference") if isinstance(msg, dict) else None
        if not isinstance(ref, dict):
            return ""
        # Flat shape: reference.text
        flat = ref.get("text")
        if isinstance(flat, str) and flat.strip():
            return flat
        # item_list shape (mirror of inbound payload)
        items = ref.get("item_list")
        if isinstance(items, list):
            parts: list[str] = []
            for it in items:
                if isinstance(it, dict) and it.get("type") == 1:
                    ti = it.get("text_item") or {}
                    t = ti.get("text") if isinstance(ti, dict) else ""
                    if isinstance(t, str):
                        parts.append(t)
            return "\n".join(p for p in parts if p)
        return ""

    def _stop_typing(self) -> None:
        """Stop + clear the per-turn TypingPing, if any. Idempotent."""
        tp = self._typing_ping
        self._typing_ping = None
        if tp is not None:
            try:
                tp.stop()
            except Exception as e:
                logger.warning("typing stop failed: %s", e)

    def _materialize_media(self, events: list[dict]) -> list[Path]:
        """C0: materialize a snapshot of media events → list of local paths.

        Events are drained from ``_pending_media`` under lock by the caller
        (``maybe_flush``); this helper is pure network IO + filesystem and
        must run OUTSIDE the state lock. Each event is downloaded +
        decrypted (image/video/file) or written as a .txt sidecar (voice
        transcript). Failures are skipped — the rest of the turn still flushes.
        """
        if not events:
            return []
        paths: list[Path] = []
        for ev in events:
            try:
                ev_paths = materialize(ev, self._ilink, self._media_dir)
            except Exception as e:
                logger.warning("materialize failed: %s", e)
                ev_paths = []
            paths.extend(ev_paths)
        return paths

    # ── recv drain ─────────────────────────────────────────────────

    def _drain_recv(self) -> str:
        """Consume provider.recv until result; mirror events into BridgeState.

        E-polish: collect thinking content blocks into ``self._last_thinking``
        so maybe_flush can emit a single 【思考】 bubble when state.thinking_on.
        """
        assert self._provider is not None
        text_chunks: list[str] = []
        thinking_chunks: list[str] = []
        for ev in self._provider.recv():
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                sid = ev.get("session_id")
                if isinstance(sid, str) and sid:
                    # Stamp session_start_ts on sid change so /info reports
                    # current cc-session age, not the bridge process age.
                    if sid != self.state.session_id:
                        cfg = self._cfg
                        self._session_created_at = (
                            get_session_created_at(cfg.session_created_command, sid)
                            if cfg is not None
                            else None
                        ) or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    self.state.session_id = sid
                    if self._last_from_wxid:
                        try:
                            self._sessions.set(self._last_from_wxid, sid)
                        except Exception as e:
                            logger.warning("sessions.set failed: %s", e)
                # Mirror cc-reported model (incl. "[1m]" suffix). Without this,
                # /info shows "?" when bridge spawned without an explicit --model.
                model = ev.get("model")
                if isinstance(model, str) and model:
                    self.state.model = model
                # B1: persist (sid, model) so /resume <sid> can recover later.
                if isinstance(sid, str) and sid:
                    try:
                        self._record_session(sid, self.state.model)
                    except Exception as e:
                        logger.warning("record_session failed: %s", e)
            elif t == "assistant":
                self._collect_assistant(ev, text_chunks, thinking_chunks)
            elif t == "stream_event":
                # cc --include-partial-messages forwards SSE deltas as
                # `stream_event` frames. Under OAuth the final assistant
                # `thinking` block is empty (signature-only); the plaintext
                # only lives in the in-flight `thinking_delta` chunks here.
                e = ev.get("event") or {}
                if e.get("type") == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "thinking_delta":
                        txt = d.get("thinking")
                        if isinstance(txt, str) and txt:
                            thinking_chunks.append(txt)
            elif t == "rate_limit_event":
                info = ev.get("rate_limit_info")
                self.state.rate_limit_info = info if isinstance(info, dict) else ev
            elif t == "result":
                usage = ev.get("usage")
                if isinstance(usage, dict):
                    self._merge_usage(usage)
                self.state.last_result_ts = self._wallclock().timestamp()
        # Mirror provider's cumulative usage if it tracks separately.
        prov_usage = getattr(self._provider, "usage_total", None)
        if isinstance(prov_usage, dict) and prov_usage:
            self.state.usage_total = dict(prov_usage)
        # B7: successful recv — provider is alive, reset consecutive death counter.
        self._provider_death_count = 0
        # Stash thinking for maybe_flush to wrap into one bubble (if enabled).
        self._last_thinking = "".join(thinking_chunks).strip()
        return "".join(text_chunks)

    def _collect_assistant(
        self,
        ev: dict,
        sink: list[str],
        thinking_sink: list[str] | None = None,
    ) -> None:
        message = ev.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            for seg in content:
                if not isinstance(seg, dict):
                    continue
                seg_type = seg.get("type")
                if seg_type == "text":
                    txt = seg.get("text")
                    if isinstance(txt, str):
                        sink.append(txt)
                elif seg_type == "thinking":
                    # Under --include-partial-messages, cc fills BOTH the
                    # stream_event thinking_delta path AND this final-frame
                    # thinking block with the same plaintext. Reading both
                    # produced a duplicated 🧠 bubble. stream_event is the
                    # source of truth — skip here.
                    pass
        elif isinstance(content, str):
            sink.append(content)
        usage = message.get("usage")
        if isinstance(usage, dict):
            self._merge_usage(usage)
            # Snapshot (overwrite) — last turn ≈ current context for /info.
            snap = {k: v for k, v in usage.items() if isinstance(v, int)}
            if snap:
                self.state.last_assistant_usage = snap

    def _merge_usage(self, usage: dict[str, Any]) -> None:
        for k, v in usage.items():
            if isinstance(v, int):
                self.state.usage_total[k] = self.state.usage_total.get(k, 0) + v

    # ── command hooks (used by commands.Registry via CommandContext) ──

    def set_registry(self, registry: Registry) -> None:
        """Wire the command registry post-construction (used by __main__)."""
        self._registry = registry

    def swap_provider(self, model: str | None, resume_sid: str | None) -> None:
        """Kill current provider, spawn a fresh one with new args, atomic swap.

        B4: use cancel() (SIGTERM → 1s wait → SIGKILL, ≤2s worst case) instead
        of close() (stdin.end → 2s → SIGTERM → 3s → SIGKILL, ≤6s worst case).
        /stop and /clear both want the old process dead immediately so the user
        does not see its trailing stdout flush for ~10s after the ack.
        """
        old = self._provider
        if old is not None:
            try:
                old.cancel()
            except Exception as e:
                logger.warning("provider cancel (swap) failed: %s", e)
        try:
            new_provider = self._provider_factory(model=model, resume_sid=resume_sid)
        except TypeError:
            # Factory does not accept kwargs (test stubs); fall back.
            new_provider = self._provider_factory()
        new_provider.spawn(env={})
        self._provider = new_provider
        self.state.usage_total = {}
        self.state.last_assistant_usage = {}

    def close_provider(self) -> None:
        """Graceful close of the current provider with no respawn."""
        if self._provider is None:
            return
        try:
            self._provider.close()
        except Exception as e:
            logger.warning("provider close failed: %s", e)
        self._provider = None

    def replay_user_text(self, text: str) -> None:
        """B9: enqueue `text` on the InboundBuffer so the flush thread runs
        a fresh cc turn — used by `/regen` after `respawn_with_resume`.

        Going through the buffer (instead of `provider.send` + `_drain_recv`
        inline) keeps all cc subprocess stdio confined to the flush thread,
        which is the single owner of the provider. It also reuses the normal
        send → drain → bubbles outbound chain, so the regenerated reply
        lands in the chat the same way any other assistant reply does.
        """
        if not text:
            return
        with self._state_lock:
            self._buffer.add(text)
            self.state.last_user_msg_ts = self._wallclock().timestamp()
        logger.info("replay_user_text enqueued for /regen (%d chars)", len(text))

    def respawn_with_resume(self, sid: str, model: str | None) -> None:
        """B9: close the live provider then spawn a fresh one with --resume <sid>.

        Used by /rewind and /regen: after the jsonl is truncated on disk, cc
        must re-read it. Closing first guarantees no race against the old
        process flushing stale events.
        """
        if self._provider is not None:
            # Suppress intermediate SessionEnd so regen/rewind doesn't archive truncated jsonl.
            _suppress = regen_suppress_path(sid)
            try:
                _suppress.touch(exist_ok=True)
            except OSError:
                pass
            try:
                self._provider.close()
            except Exception as e:
                logger.warning("respawn close failed: %s", e)
            self._provider = None
        self._provider_death_count = 0
        try:
            new_provider = self._provider_factory(model=model, resume_sid=sid)
        except TypeError:
            new_provider = self._provider_factory()
        try:
            new_provider.spawn(env={})
        except TypeError:
            new_provider.spawn()
        self._provider = new_provider
        # Mirror swap_provider's usage reset — fresh cc means fresh /info ctx.
        self.state.usage_total = {}
        self.state.last_assistant_usage = {}

    def get_status(self) -> dict:
        """B10: snapshot for /status — cc pid, cwd, iLink ok, sid, uptime."""
        prov = self._provider
        pid: int | None = None
        cwd: str | None = None
        if prov is not None:
            proc = getattr(prov, "process", None)
            if proc is not None and getattr(proc, "pid", None) is not None:
                if proc.poll() is None:
                    pid = proc.pid
            cwd = getattr(prov, "cwd", None)
        now = self._clock()
        # iLink "ok" iff a successful poll happened within 3 polling intervals.
        ilink_ok = (
            self._last_poll_ok_ts > 0
            and (now - self._last_poll_ok_ts) <= max(3.0, self._poll_interval_sec * 3)
        )
        session_age = None
        if self._session_created_at:
            try:
                created = datetime.fromisoformat(self._session_created_at.replace("Z", "+00:00"))
                session_age = (datetime.now(timezone.utc) - created).total_seconds()
            except (ValueError, TypeError):
                pass
        return {
            "cc_pid": pid,
            "cwd": cwd,
            "ilink_ok": ilink_ok,
            "last_active_sid": self.state.session_id,
            "session_age_sec": session_age,
        }

    def forget_session(self) -> None:
        """Drop the current user's sid from the SessionTracker."""
        # Clear so /info shows '?' until cc emits the next system{init}.
        self._session_created_at = None
        wxid = self._last_from_wxid
        if not wxid:
            return
        try:
            self._sessions.forget(wxid)
        except Exception as e:
            logger.warning("sessions.forget failed: %s", e)

    # ── failure path ──────────────────────────────────────────────

    def _handle_provider_dead(
        self, err: Exception, from_wxid: str | None, ctx_token: str
    ) -> None:
        sid = self.state.session_id
        if sid:
            # B7: count consecutive deaths when session is alive (previously
            # fully suppressed). Alert + bubble only after 3rd consecutive death.
            self._provider_death_count += 1
            logger.info(
                "provider dead, session alive (count=%d): %s",
                self._provider_death_count,
                err,
            )
            if self._provider_death_count >= 3:
                logger.error(
                    "provider dead %d consecutive times (sid=%s): %s",
                    self._provider_death_count,
                    sid,
                    err,
                )
                if self._alerts is not None:
                    try:
                        self._alerts.write(
                            "critical",
                            "provider_dead",
                            f"consecutive deaths={self._provider_death_count} sid={sid}: {err}",
                            source="loop._handle_provider_dead",
                            fingerprint="provider.dead",
                        )
                    except Exception as ae:
                        logger.warning("alerts.write failed: %s", ae)
                else:
                    self._write_alert(f"provider_dead: {err}")
                if from_wxid:
                    try:
                        self._ilink.send_text(
                            from_wxid,
                            ctx_token,
                            messages.t("provider.dead", self.state.voice_style),
                        )
                    except Exception as e:
                        logger.warning("fallback send_text failed: %s", e)
            return
        # session_id not set → real unrecoverable death, alert immediately.
        self._provider_death_count += 1
        logger.error("provider dead, session gone: %s", err)
        if self._alerts is not None:
            try:
                self._alerts.write(
                    "critical",
                    "provider_dead",
                    str(err),
                    source="loop._drain_recv",
                    fingerprint="provider.dead",
                )
            except Exception as e:
                logger.warning("alerts.write failed: %s", e)
        else:
            self._write_alert(f"provider_dead: {err}")
        if from_wxid:
            try:
                self._ilink.send_text(
                    from_wxid,
                    ctx_token,
                    messages.t("provider.dead", self.state.voice_style),
                )
            except Exception as e:
                logger.warning("fallback send_text failed: %s", e)

    def _write_alert(self, body: str) -> None:
        try:
            self._alert_dir.mkdir(parents=True, exist_ok=True)
            ts = int(self._wallclock().timestamp())
            path = self._alert_dir / f"{ts}.txt"
            path.write_text(body)
        except OSError as e:
            logger.warning("alert write failed: %s", e)
