"""Bridge-owned slash command + natural alias dispatch.

Match order (first match wins):
  1. Slash handlers: /info /model /clear /stop (unknown slash → error reply).
  2. Literal commands: ``mm-`` / ``mm+`` (B8). Whole-message-only, lowercase-only.
  3. Natural alias: text matches a MODEL_ALIASES key (case-insensitive, no other).
  4. Forward → provider.

All user-facing ack strings go through ``messages.t(key, style, **vars)`` —
inline f-string acks are forbidden. New ack = register key in messages.py
(both ``cn`` + ``en``) first; full contract in MAP.md §3 Outbound.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from synapse_core import replay_bookmark, replay, session_lock
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from synapse_core import jsonl_edit
from synapse_core.state import BridgeState
from synapse_core.usage import Usage
from synapse_core.commands import messages
from synapse_core.commands.aliases import MODEL_ALIASES, display_name, resolve_model

logger = logging.getLogger(__name__)

DispatchResult = tuple[Literal["handled", "forward"], str | None]

_CTX_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

# B8: literal (no leading slash) commands. Exact-match, lowercase-only — they
# steal a whole inbound message, so case-sensitivity prevents collisions with
# user prose like "MM-don't archive this".
_MM_MINUS = "mm-"
_MM_PLUS = "mm+"

# /effort: official cc 2.1.159+ levels passed verbatim as `--effort <level>`.
_EFFORT_LEVELS: frozenset[str] = frozenset(
    {"low", "medium", "high", "xhigh", "max", "ultracode", "auto"}
)

# /cwd presets. Index = digit shown to user; preset 1 is the boot fallback
# when persisted state.cc_cwd no longer exists on disk. Loaded from
# SYNAPSE_CWD_PRESETS env var (colon-separated paths); falls back to empty
# tuple when unset so the caller sees "no presets" rather than hardcoded paths.
_CWD_PRESETS: tuple[str, ...] = tuple(
    p for p in os.environ.get("SYNAPSE_CWD_PRESETS", "").split(":") if p.strip()
)


def _fmt_last_active(raw: str | None) -> str:
    """Render sessions.last_active (ISO-8601 UTC `Z`) as local HH:MM.

    Returns an empty string on parse failure so the picker line just drops
    the timestamp instead of leaking a parse error.
    """
    if not raw:
        return ""
    try:
        ts = raw.rstrip("Z")
        dt_utc = datetime.fromisoformat(ts).replace(tzinfo=UTC)
        return dt_utc.astimezone().strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return ""


def _format_resume_picker(rows: list[dict]) -> str:
    """B6: render the N-most-recent picker (lines numbered 1..N).

    Per-row layout: ``{i}. [{ch}·{project}] {title} ({sid8}) {model} {HH:MM}``.
    Title falls back to a placeholder when the first prompt has not been
    captured yet; HH:MM is omitted when last_active is missing/malformed.
    """
    out = ["Recent sessions:"]
    for i, r in enumerate(rows, 1):
        sid_full = r.get("sid") or ""
        sid = sid_full[:8]
        ch = session_lock.holder(sid_full) or r.get("channel") or "-"
        cwd = r.get("cwd") or ""
        project = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
        tag = f"[{ch}·{project}]" if project else f"[{ch}]"
        title = (r.get("title") or "").strip() or "(untitled)"
        model = display_name(r.get("model") or None)
        hh_mm = _fmt_last_active(r.get("last_active"))
        tail = f" {hh_mm}" if hh_mm else ""
        out.append(f"{i}. {tag} {title} ({sid}) {model}{tail}")
    out.append("Reply with the number (e.g. 1) to resume.")
    return "\n".join(out)


def _health_word(snap: dict) -> str:
    """Same matrix B10 /status used — 4 quadrants of (ilink, cc) liveness."""
    ilink_ok = bool(snap.get("ilink_ok"))
    cc_alive = snap.get("cc_pid") is not None
    if ilink_ok and cc_alive:
        return "ok"
    if not ilink_ok and not cc_alive:
        return "down"
    if not ilink_ok:
        return "no-poll"
    return "cc-dead"


def _fmt_uptime(sec: float | None) -> str:
    if sec is None or sec < 0:
        return "?"
    s = int(sec)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d{h:02d}h"


@dataclass
class CommandContext:
    """Closures the registry calls on MainLoop.

    Passing closures (not the loop itself) keeps the registry testable
    without spinning a real MainLoop.
    """

    state: BridgeState
    swap_provider: Callable[[str | None, str | None], None]
    close_provider: Callable[[], None]
    forget_session: Callable[[], None]
    # User-initiated sessionend trigger: /clear and /cwd call this with the
    # old sid BEFORE close+swap so marrow's sessionend_async (LLM pipeline +
    # affect/digest extraction) runs. cc's SessionEnd hook in bridge
    # mode skips this popen by design — the bridge owns the timing.
    fire_sessionend: Callable[[str], None] = field(
        default_factory=lambda: lambda _sid: None
    )
    # B10: status accessors. Each returns the current value or None when
    # unavailable. Defaults give sensible no-data values so test stubs work.
    get_status: Callable[[], dict] = field(default_factory=lambda: lambda: {})
    commands_doc_path: Path | None = None
    # B1: marrow sessions integration. Defaults are no-op so tests work.
    resolve_resume_model: Callable[[str], str | None] = field(
        default_factory=lambda: lambda _sid: None
    )
    clear_default_model: str = "claude-opus-4-6[1m]"
    # B6: recent-session picker for empty /resume.
    list_recent_sessions: Callable[[], list[dict]] = field(
        default_factory=lambda: lambda: []
    )
    # Persist `model`/`effort_level`/`thinking_on`/`voice_style` after every
    # mutation so a bridge crash does not silently revert /effort etc. Default
    # no-op for tests that don't care about persistence.
    persist_state: Callable[[], None] = field(
        default_factory=lambda: lambda: None
    )
    # B8: marrow audit_log writer for ``mm-`` / ``mm+``. Signature is
    # ``(kind, sid, status)`` where ``kind`` is ``"manual_skip"`` or
    # ``"session_block"`` and ``status`` is one of:
    #   manual_skip:    "skip" | "skip_cleared"
    #   session_block:  "archive" | "cleared"
    # Default is a no-op so the bridge runs even when marrow is offline.
    audit_writer: Callable[[str, str, str], None] = field(
        default_factory=lambda: lambda _kind, _sid, _status: None
    )
    # B6: history replay reader. Returns WeChat-ready `[回放]` bubbles for sid.
    # Empty list = nothing to replay; registry then skips the bubble push but
    # still acks + swaps.
    replay_for_sid: Callable[[str], list[str]] = field(
        default_factory=lambda: lambda _sid: []
    )
    # B6: emit pre-formatted bubbles (replay) ahead of the ack reply so the
    # user sees `[回放]` lines arrive first. Default no-op for tests.
    send_extra_bubbles: Callable[[list[str]], None] = field(
        default_factory=lambda: lambda _bubbles: None
    )
    # B9: respawn hook for /rewind + /regen. Closes the live cc subprocess and
    # spawns a fresh one with `--resume <sid>` so cc re-reads the truncated
    # jsonl. Default is a no-op so registry tests can run without a MainLoop.
    respawn_with_resume: Callable[[str, str | None], None] = field(
        default_factory=lambda: lambda _sid, _model: None
    )
    # B9: replay hook for /regen. After truncating the last user/assistant pair
    # and respawning cc, the bridge resends the dropped user prompt on stdin so
    # cc actually regenerates — `--resume` alone does not auto-replay the
    # trailing unanswered prompt. Default no-op for tests + bridges that lack
    # a live provider.
    replay_user_text: Callable[[str], None] = field(
        default_factory=lambda: lambda _text: None
    )
    # B9: cc cwd used to locate the per-sid jsonl
    # (`~/.claude/projects/<slug>/<sid>.jsonl`). Default None → jsonl_edit
    # falls back to `os.getcwd()` then walks `~/.claude/projects/`.
    cc_cwd: str | None = None
    # Channel identity for session lock (e.g. "tg", "wx").
    channel: str = ""
    # B9: override the projects root (`~/.claude/projects/`). Used by tests to
    # point jsonl_edit at a sandbox tmp dir. None → real default.
    cc_projects_root: Path | None = None
    # E-polish /compact: pipe `/compact` literally into cc's stream-json stdin
    # via the loop's wired handler. Returns the user-facing ack string. Default
    # no-op so dispatch still answers when the bridge is not wired (e.g. tests
    # without a loop). Implementations may raise; registry catches + falls back.
    compact_handler: Callable[[], str] = field(
        default_factory=lambda: lambda: messages.t("compact.ok", "cn")
    )
    # /info real usage%: oauth /api/oauth/usage cached fetcher. Returns Usage
    # snapshot or None (no token / network down / cold-start 429). Default
    # no-op so tests + bridges without the oauth wiring fall back to the
    # legacy `?(5h) ?(7d)` placeholder.
    usage_client: Callable[[], Usage | None] = field(
        default_factory=lambda: lambda: None
    )
    # cwd resolver for /resume: returns the stored cwd for a sid, or None.
    # Default no-op so tests without marrow wiring leave cwd unchanged.
    resolve_session_cwd: Callable[[str], str | None] = field(
        default_factory=lambda: lambda _sid: None
    )
    resolve_session_effort: Callable[[str], str | None] = field(
        default_factory=lambda: lambda _sid: None
    )
    record_effort: Callable[[str, str], None] = field(
        default_factory=lambda: lambda _sid, _effort: None
    )
    # /diary: fetch diary content by date string. Returns (content, label) or
    # (None, None). None = not wired (bridge omits the closure).
    fetch_diary: Callable[[str], tuple[str | None, str | None]] | None = None


class Registry:
    """Match raw inbound user text against bridge handlers + aliases."""

    def __init__(self, ctx: CommandContext) -> None:
        self._ctx = ctx
        self._pending_rewrite: str | None = None

    @property
    def pending_rewrite(self) -> str | None:
        """Pop rewrite text set by last dispatch (e.g. diary inject)."""
        r = self._pending_rewrite
        self._pending_rewrite = None
        return r

    def _t(self, key: str, **vars: object) -> str:
        """Render an ack in the current voice style."""
        return messages.t(key, self._ctx.state.voice_style, **vars)

    def dispatch(self, raw: str) -> DispatchResult:
        self._pending_rewrite = None
        if raw is None:
            return ("forward", None)
        text = raw.strip()
        if not text:
            return ("forward", None)

        # Picker state machine: read + clear at entry so any inbound message
        # ends the picker window. _handle_resume re-arms after rendering.
        state = self._ctx.state
        pending = state.pending_picker
        if pending is not None:
            state.pending_picker = None

        if text.startswith("/"):
            state.picker_rows = []
            return ("handled", self._dispatch_slash(text[1:]))

        # Bare digit right after a /resume picker → route to picker handler.
        # picker_rows is consumed inside _handle_resume, not cleared here.
        if pending == "resume" and text.isdigit():
            return ("handled", self._handle_resume(text))
        # Same for /cwd picker: bare digit picks a preset.
        if pending == "cwd" and text.isdigit():
            state.picker_rows = []
            return ("handled", self._handle_cwd(text))

        state.picker_rows = []

        # B8: bare-message literals. Exact match only — payload after the
        # literal forwards as prose so users can still write "mm- this" freely.
        if text == _MM_MINUS:
            return ("handled", self._handle_mm_minus())
        if text == _MM_PLUS:
            return ("handled", self._handle_mm_plus())

        # Natural alias path: bare alias matches a key in MODEL_ALIASES.
        if text.lower() in MODEL_ALIASES:
            return ("handled", self._handle_model(text))

        # Cross-channel session lock: auto-clear if another channel claimed this sid.
        ch = self._ctx.channel
        sid = state.session_id
        if ch and sid:
            owner = session_lock.holder(sid)
            if owner and owner != ch:
                try:
                    replay_bookmark.save(sid, ch, self._ctx.cc_cwd)
                except Exception:
                    pass
                self._ctx.close_provider()
                self._ctx.forget_session()
                state.session_id = None
                default_model = self._ctx.clear_default_model or state.model
                state.model = default_model
                self._ctx.swap_provider(default_model, None)
                self._ctx.persist_state()
                effort = (state.effort_level or "high").capitalize()
                try:
                    self._ctx.send_extra_bubbles(
                        [self._t("session.claimed_away", channel=owner,
                                 name=display_name(default_model), effort=effort)]
                    )
                except Exception:
                    pass
                return ("forward", None)

        return ("forward", None)

    # ── slash routing ─────────────────────────────────────────────

    def _dispatch_slash(self, body: str) -> str:
        head, _, rest = body.partition(" ")
        name = head.lower()
        if name in ("info", "status", "usage"):
            return self._handle_info()
        if name == "model":
            return self._handle_model(rest)
        if name in ("clear", "new"):
            return self._handle_clear()
        if name == "stop":
            return self._handle_stop()
        if name == "help":
            return self._handle_help()
        if name == "resume":
            return self._handle_resume(rest)
        if name == "rewind":
            return self._handle_rewind(rest)
        if name == "regen":
            return self._handle_regen()
        if name == "thinking":
            return self._handle_thinking(rest)
        if name == "quote":
            return self._handle_quote(rest)
        if name == "effort":
            return self._handle_effort(rest)
        if name == "compact":
            return self._handle_compact()
        if name == "voice":
            return self._handle_voice(rest)
        if name == "cwd":
            return self._handle_cwd(rest)
        if name == "diary":
            return self._handle_diary(rest)
        return self._t("unknown.cmd", x=head)

    # ── handlers ──────────────────────────────────────────────────

    def _handle_info(self) -> str:
        """One-bubble combined snapshot — triggered by /info, /status, /usage.

        Two lines, `\\n`-joined (WeChat renders as a single bubble):
          line 1: ``model | cwd | Health:<word>``
          line 2: ``<sid8> | <session_age> | 53%(5h) 17%(7d) | <ctx>k``

        - sid renders the first 8 chars only, or ``—`` when no session yet
          (no ``SID-`` prefix — the field position carries the meaning).
        - session_age is the real session lifespan from marrow created_at,
          falling back to bridge-init time if marrow is unavailable. ``?`` until
          the first sid arrives.
        - 5h / 7d come from oauth ``/api/oauth/usage`` via UsageClient,
          cached, never raises. Per-window fallback to the legacy
          rate_limit_event hours-until-reset (5h) and ``?(7d)`` placeholder.
        - Tokens: current context ≈ last assistant turn's
          input + cache_read + cache_creation (NOT cumulative — cache_read
          repeats across turns and inflates by N× otherwise).
        """
        state = self._ctx.state
        snap = self._safe_status()

        model_disp = display_name(state.model)
        effort = state.effort_level or "high"
        cwd = snap.get("cwd") or "?"
        health = _health_word(snap)
        line1 = f"{model_disp}[{effort}] | {cwd} | Health:{health}"

        sid_disp = state.session_id[:8] if state.session_id else "SID"
        session_age = _fmt_uptime(snap.get("session_age_sec"))
        usage = self._safe_usage()
        five_h = self._format_usage_pct(
            usage.five_hour_pct if usage else None, "5h"
        ) or self._format_five_hour(state.rate_limit_info)
        seven_d = self._format_usage_pct(
            usage.seven_day_pct if usage else None, "7d"
        ) or "?(7d)"
        tokens = self._format_tokens(state.last_assistant_usage)
        line2 = f"{sid_disp} | {session_age} | {five_h} {seven_d} | {tokens}"

        return f"{line1} | {line2}"

    def _safe_usage(self) -> Usage | None:
        try:
            return self._ctx.usage_client()
        except Exception:
            return None

    def _safe_status(self) -> dict:
        try:
            return self._ctx.get_status() or {}
        except Exception:
            return {}

    @staticmethod
    def _format_usage_pct(pct: float | None, window: str) -> str | None:
        if pct is None:
            return None
        return f"{pct:.0f}%({window})"

    def _handle_model(self, rest: str) -> str:
        state = self._ctx.state
        token = (rest or "").strip()
        if not token:
            return self._t("model.usage")
        canonical = resolve_model(token) or token
        name = display_name(canonical)
        if canonical == state.model:
            return self._t("model.same", name=name)
        self._ctx.swap_provider(canonical, state.session_id)
        state.model = canonical
        self._ctx.persist_state()
        return self._t("model.ok", name=name)

    def _handle_clear(self) -> str:
        state = self._ctx.state
        # B1: every /clear lands on opus-4.6[1m] (or whatever ctx says) so the
        # user does not silently stay on the last session's model. Both
        # effort_level and thinking_on persist across /clear — user prefs
        # stick, only model resets (0614).
        default_model = self._ctx.clear_default_model or state.model
        # Fire sessionend BEFORE close+swap so the old sid actually goes through
        # marrow's LLM pipeline (lifecycle:end, affect, digest). cc's
        # SessionEnd hook in bridge mode only writes archive + bridge_owns
        # marker; without this popen the sid stays orphaned forever.
        old_sid = state.session_id
        if old_sid:
            try:
                replay_bookmark.save(old_sid, self._ctx.channel or "", self._ctx.cc_cwd)
            except Exception:
                pass
            try:
                self._ctx.fire_sessionend(old_sid)
            except Exception:  # noqa: BLE001 — never block /clear
                pass
            if self._ctx.channel:
                session_lock.release(old_sid, self._ctx.channel)
        self._ctx.forget_session()
        state.session_id = None
        state.model = default_model
        self._ctx.swap_provider(default_model, None)
        self._ctx.persist_state()
        effort = (state.effort_level or "high").capitalize()
        return self._t("clear.ok", name=display_name(default_model), effort=effort)

    def _handle_resume(self, rest: str) -> str:
        """B1: /resume <sid> reads model from marrow.sessions (fallback jsonl
        grep) and respawns cc with --resume <sid>. Empty arg / digit forms are
        added in B6."""
        token = (rest or "").strip()
        if not token:
            rows = self._ctx.list_recent_sessions() or []
            if not rows:
                return self._t("resume.empty")
            # Arm the picker and snapshot rows so a delayed digit reply
            # resolves against the same list the user saw.
            self._ctx.state.pending_picker = "resume"
            self._ctx.state.picker_rows = rows
            return _format_resume_picker(rows)
        if token.isdigit():
            rows = self._ctx.state.picker_rows or self._ctx.list_recent_sessions() or []
            idx = int(token) - 1
            if idx < 0 or idx >= len(rows):
                return self._t("resume.no_n", n=token)
            sid = rows[idx].get("sid") or ""
            if not sid:
                return self._t("resume.no_n", n=token)
            self._ctx.state.picker_rows = []
            return self._resume_sid(sid)
        return self._resume_sid(token)

    def _resume_sid(self, sid: str) -> str:
        state = self._ctx.state
        # If this bridge has a different active session, clear it first.
        old_sid = state.session_id
        if old_sid and old_sid != sid:
            try:
                replay_bookmark.save(old_sid, self._ctx.channel or "", self._ctx.cc_cwd)
            except Exception:
                pass
            try:
                self._ctx.fire_sessionend(old_sid)
            except Exception:
                pass
            if self._ctx.channel:
                session_lock.release(old_sid, self._ctx.channel)
            self._ctx.forget_session()
        # If target sid is held by another channel, fire its sessionend.
        holder = session_lock.holder(sid)
        if holder and holder != (self._ctx.channel or ""):
            try:
                self._ctx.fire_sessionend(sid)
            except Exception:
                pass
        resolved = self._ctx.resolve_resume_model(sid)
        if resolved:
            branch = "resolved"
            model = resolved
        elif state.model:
            branch = "state.model"
            model = state.model
        else:
            branch = "clear_default"
            model = self._ctx.clear_default_model
        logger.info(
            "/resume sid=%s model=%s branch=%s", sid[:8], model, branch
        )
        # B6: emit `[回放]` bubbles BEFORE the swap so the user sees history
        # arrive ahead of the ack. Replay errors are swallowed — the resume
        # itself must not fail just because the jsonl is gone.
        try:
            bm = replay_bookmark.load(sid, self._ctx.channel or "")
            if bm is not None:
                raw_turns = replay.read_turns_since(sid, bm, cwd=self._ctx.cc_cwd)
                bubbles = replay.format_for_channel(raw_turns) if raw_turns else []
            else:
                bubbles = self._ctx.replay_for_sid(sid) or []
        except Exception:
            bubbles = []
        if bubbles:
            try:
                self._ctx.send_extra_bubbles(bubbles)
            except Exception:
                pass
        # Resolve cwd for the target session and update state BEFORE swap so
        # provider_factory reads the new cwd when spawning the resumed cc.
        cwd_ack: str | None = None
        target_cwd = self._ctx.resolve_session_cwd(sid)
        if target_cwd and os.path.isdir(target_cwd) and target_cwd != state.cc_cwd:
            state.cc_cwd = target_cwd
            self._ctx.cc_cwd = target_cwd
            cwd_ack = self._t("resume.cwd_switched", dir=os.path.basename(target_cwd))
        target_effort = self._ctx.resolve_session_effort(sid)
        if target_effort:
            state.effort_level = target_effort
        self._ctx.swap_provider(model, sid)
        state.model = model
        state.session_id = sid
        if self._ctx.channel:
            session_lock.claim(sid, self._ctx.channel)
        self._ctx.persist_state()
        effort = (state.effort_level or "high").capitalize()
        ack = self._t("resume.ok", sid=sid[:8], name=display_name(model), effort=effort)
        if cwd_ack:
            ack = f"{ack}\n{cwd_ack}"
        return ack

    def _handle_stop(self) -> str:
        state = self._ctx.state
        self._ctx.swap_provider(state.model, state.session_id)
        return self._t("stop.ok")

    # ── B8: mm- / mm+ literal commands ────────────────────────────

    def _handle_mm_minus(self) -> str:
        """Block the current session from marrow ingest.

        Writes two ``audit_log`` rows (latest-wins on the marrow read side):
          - ``manual_skip = skip``   → ``sessionend_async`` skips the LLM pipeline.
          - ``session_block = archive`` → events archive blocks insert entirely.
        """
        sid = self._ctx.state.session_id
        if not sid:
            return self._t("mm.block_no_sess")
        self._ctx.audit_writer("manual_skip", sid, "skip")
        self._ctx.audit_writer("session_block", sid, "archive")
        return self._t("mm.block")

    def _handle_mm_plus(self) -> str:
        """Reverse a prior ``mm-`` for the current session.

        Writes a new pair of rows; marrow's latest-wins reader picks them up.
        """
        sid = self._ctx.state.session_id
        if not sid:
            return self._t("mm.clear_no_sess")
        self._ctx.audit_writer("manual_skip", sid, "skip_cleared")
        self._ctx.audit_writer("session_block", sid, "cleared")
        return self._t("mm.clear")

    def _handle_help(self) -> str:
        """B10: render COMMANDS.md body."""
        path = self._ctx.commands_doc_path
        if path is None:
            path = Path(__file__).resolve().parents[2] / "COMMANDS.md"
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError:
            return self._t("help.missing")
        return body

    # ── B9: /rewind + /regen ──────────────────────────────────────

    def _handle_rewind(self, rest: str) -> str:
        """Drop the last N user/assistant pairs from sid's jsonl, then respawn
        cc with ``--resume <sid>``. Dropped turns are flagged in marrow
        ``audit_log`` (session_block=archive) so they never ingest as events.
        """
        token = (rest or "").strip()
        if not token:
            return self._t("rewind.usage")
        try:
            n = int(token)
        except ValueError:
            return self._t("rewind.bad_n")
        if n <= 0:
            return self._t("rewind.bad_n")
        state = self._ctx.state
        sid = state.session_id
        if not sid:
            return self._t("rewind.no_sess")
        dropped = jsonl_edit.drop_last_n_pairs(
            sid, n, cwd=self._ctx.cc_cwd, projects_root=self._ctx.cc_projects_root
        )
        if dropped:
            try:
                self._ctx.audit_writer("session_block", sid, "archive")
            except Exception:
                pass
        try:
            self._ctx.respawn_with_resume(sid, state.model)
        except Exception:
            pass
        if dropped:
            try:
                self._ctx.audit_writer("session_block", sid, "cleared")
            except Exception:
                pass
        real_n = sum(1 for d in dropped if jsonl_edit.is_real_user_prompt(d))
        return self._t("rewind.ok", n=real_n)

    def _handle_regen(self) -> str:
        """Drop the last user/assistant pair, respawn cc, resend the dropped
        user prompt so cc regenerates a fresh reply.

        cc's `--resume <sid>` only reloads context; it does NOT auto-replay
        the trailing unanswered prompt. So we truncate the pair off disk,
        respawn (cc reads the trimmed jsonl), then push the user's text back
        on stdin via ``replay_user_text`` — that's what actually triggers a
        new generation.
        """
        state = self._ctx.state
        sid = state.session_id
        if not sid:
            return self._t("regen.no_sess")
        dropped = jsonl_edit.drop_last_n_pairs(
            sid, 1, cwd=self._ctx.cc_cwd, projects_root=self._ctx.cc_projects_root
        )
        replay_text: str | None = None
        for ev in dropped:
            text = jsonl_edit.extract_user_text(ev)
            if text:
                replay_text = text
                break
        if dropped:
            try:
                self._ctx.audit_writer("session_block", sid, "archive")
            except Exception:
                pass
        try:
            self._ctx.respawn_with_resume(sid, state.model)
        except Exception:
            pass
        if dropped:
            try:
                self._ctx.audit_writer("session_block", sid, "cleared")
            except Exception:
                pass
        if replay_text:
            try:
                self._ctx.replay_user_text(replay_text)
            except Exception:
                pass
        return self._t("regen.ok")

    # ── E-polish: /thinking + /effort + /compact ─────────────────

    def _handle_thinking(self, rest: str) -> str:
        """Flip BridgeState.thinking_on. ``on`` / ``off`` only."""
        token = (rest or "").strip().lower()
        if not token:
            current = "on" if self._ctx.state.thinking_on else "off"
            return self._t("thinking.usage", x=current)
        if token == "on":
            self._ctx.state.thinking_on = True
            self._ctx.persist_state()
            return self._t("thinking.on")
        if token == "off":
            self._ctx.state.thinking_on = False
            self._ctx.persist_state()
            return self._t("thinking.off")
        current = "on" if self._ctx.state.thinking_on else "off"
        return self._t("thinking.usage", x=current)

    def _handle_quote(self, rest: str) -> str:
        """Flip BridgeState.quote_on. ``on`` / ``off`` only.

        Off (default): cc's <quote> tag is stripped from the reply, no
        decorative ▎FRAGMENT bubble is prepended. On: bubble prepended.
        """
        token = (rest or "").strip().lower()
        if not token:
            current = "on" if self._ctx.state.quote_on else "off"
            return self._t("quote.usage", x=current)
        if token == "on":
            self._ctx.state.quote_on = True
            self._ctx.persist_state()
            return self._t("quote.on")
        if token == "off":
            self._ctx.state.quote_on = False
            self._ctx.persist_state()
            return self._t("quote.off")
        current = "on" if self._ctx.state.quote_on else "off"
        return self._t("quote.usage", x=current)

    def _handle_effort(self, rest: str) -> str:
        """Validate the level against the official cc 7-tuple and store on
        BridgeState.effort_level. The loop's provider_factory reads it on the
        next swap and passes it through to ClaudeCodeProvider as
        ``--effort <level>``.
        """
        token = (rest or "").strip().lower()
        if not token:
            current = self._ctx.state.effort_level
            return self._t("effort.usage", x=current)
        if token not in _EFFORT_LEVELS:
            current = self._ctx.state.effort_level
            return self._t("effort.usage", x=current)
        self._ctx.state.effort_level = token
        self._ctx.persist_state()
        sid = self._ctx.state.session_id
        if sid:
            self._ctx.record_effort(sid, token)
        return self._t("effort.ok", level=token)

    def _handle_compact(self) -> str:
        """Pipe `/compact` to cc via the wired handler; fall back on failure."""
        sid = self._ctx.state.session_id
        if not sid:
            return self._t("compact.no_sess")
        handler = self._ctx.compact_handler
        try:
            reply = handler()
        except Exception as e:
            return self._t("compact.fail", error=e)
        return reply or self._t("compact.ok")

    def _handle_voice(self, rest: str) -> str:
        """Swap BridgeState.voice_style between ``cn`` and ``en``.

        The ack is always rendered in the NEW style (post-swap) — that way the
        user sees a sample of what they just chose. ``/voice`` alone shows
        current value + usage.
        """
        token = (rest or "").strip().lower()
        state = self._ctx.state
        if not token:
            return messages.t("voice.usage", state.voice_style, x=state.voice_style)
        if token not in messages.STYLES:
            return messages.t("voice.usage", state.voice_style, x=state.voice_style)
        if token == state.voice_style:
            return messages.t("voice.same", state.voice_style, x=token)
        state.voice_style = token
        self._ctx.persist_state()
        # Render the welcome line in the new style so the user immediately
        # sees the tone they just switched to.
        return messages.t("voice.set", token)

    def _handle_cwd(self, rest: str) -> str:
        """Show current cwd + presets, or switch the cc subprocess to a new cwd.

        Switching implies an implicit /clear: the live cc is killed and a
        fresh one is spawned at the new path so the project_slug + tooling
        match the new directory. Persists state.cc_cwd.
        """
        state = self._ctx.state
        token = (rest or "").strip()
        if not token:
            # Arm the picker so a bare digit in the next message picks a preset.
            state.pending_picker = "cwd"
            return self._t("cwd.show", cur=state.cc_cwd or "?")
        if token.isdigit():
            idx = int(token) - 1
            if idx < 0 or idx >= len(_CWD_PRESETS):
                return self._t("cwd.no_n", n=token)
            new_path = _CWD_PRESETS[idx]
        else:
            try:
                resolved = Path(token).expanduser().resolve()
            except (OSError, RuntimeError):
                return self._t("cwd.not_found")
            if not resolved.exists():
                return self._t("cwd.not_found")
            if not resolved.is_dir():
                return self._t("cwd.not_dir")
            new_path = str(resolved)
        state.cc_cwd = new_path
        # Keep ctx.cc_cwd (used by /rewind, /regen for jsonl lookup) in sync.
        self._ctx.cc_cwd = new_path
        # Implicit /clear: drop the live session and spawn a fresh cc at new
        # cwd. Mirror _handle_clear's reset of session-scoped runtime fields
        # so the new session starts clean. effort_level + thinking_on
        # persist (0614).
        default_model = self._ctx.clear_default_model or state.model
        # Fire sessionend for the old sid so marrow's LLM pipeline runs before
        # we swap it out. Same rationale as _handle_clear.
        old_sid = state.session_id
        if old_sid:
            try:
                self._ctx.fire_sessionend(old_sid)
            except Exception:  # noqa: BLE001 — never block /cwd
                pass
        self._ctx.swap_provider(default_model, None)
        self._ctx.forget_session()
        state.session_id = None
        state.model = default_model
        self._ctx.persist_state()
        return self._t("cwd.ok", name=Path(new_path).name or new_path)

    def _handle_diary(self, rest: str) -> str:
        rest = rest.strip()
        if not rest:
            return self._t("diary.noparam")
        if not self._ctx.fetch_diary:
            return self._t("diary.unavail")
        content, label = self._ctx.fetch_diary(rest)
        if not content:
            return self._t("diary.empty", date=rest)
        self._pending_rewrite = (
            f"[DIARY — {label}]\n{content}\n"
            "[No need to restate the whole diary unless I explicitly ask. "
            "Respond naturally with your comments and feelings.]"
        )
        return self._t("diary.ok", date=label)

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _format_five_hour(info: dict | None) -> str:
        if not isinstance(info, dict):
            return "?(5h)"
        # rate_limit_event payloads either have rate_limit_info nested or are flat.
        nested = info.get("rate_limit_info")
        inner = nested if isinstance(nested, dict) else info
        if inner.get("rateLimitType") != "five_hour":
            return "?(5h)"
        resets_at = inner.get("resetsAt")
        if not isinstance(resets_at, (int, float)):
            return "?(5h)"

        delta = float(resets_at) - time.time()
        if delta <= 0:
            return "0h(5h)"
        hours = delta / 3600.0
        return f"~{hours:.1f}h(5h)"

    @staticmethod
    def _format_tokens(usage: dict[str, int]) -> str:
        if not usage:
            return "0.0k"
        total = 0
        for key in _CTX_KEYS:
            v = usage.get(key)
            if isinstance(v, int):
                total += v
        return f"{total / 1000:.1f}k"
