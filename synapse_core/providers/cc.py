from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import Provider
from .errors import ProviderDeadError, ProviderSpawnError, ProviderStallError

log = logging.getLogger(__name__)


def _drain_stderr(
    stderr_pipe,
    log_path: Path,
) -> None:
    """Daemon thread: read cc stderr line-by-line and append to log_path."""
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            for raw_line in stderr_pipe:
                line = raw_line.rstrip("\n")
                if not line.strip():
                    continue
                ts = datetime.now(UTC).isoformat(timespec="seconds")
                try:
                    fh.write(f"{ts} {line}\n")
                    fh.flush()
                except Exception as exc:
                    log.warning("cc-stderr-drain write failed: %s", exc)
    except Exception as exc:
        log.warning("cc-stderr-drain thread error: %s", exc)
    finally:
        try:
            stderr_pipe.close()
        except Exception:
            pass

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

# Liveness defaults (seconds of CONTINUOUS silence between stream events).
# Overridable per bridge via config.toml; bridges pass explicit values.
_DEFAULT_IDLE_SOFT_S = 60.0
_DEFAULT_IDLE_HARD_S = 300.0

# Per-turn OUTPUT token brake: cancel a runaway turn (e.g. a huge thinking
# spiral) instead of burning quota for 15 minutes. Overridable per bridge via
# config.toml; 0 or negative disables. Counts ONLY newly produced output
# tokens for the current turn — never input/cache figures (those reflect
# window size and would false-trigger every turn).
_DEFAULT_TURN_OUTPUT_CAP = 20000

# Sentinel objects for the stdout reader queue: EOF = clean stream end,
# an Exception instance = a read error surfaced to recv().
_STDOUT_EOF = object()

# poll_line sentinel: the reader thread reported EOF or a read error while the
# caller was polling idle. Distinct from None (no line within the timeout).
POLL_EOF = object()


def _read_stdout_to_queue(stdout_pipe, q: "queue.Queue") -> None:
    """Daemon thread: push each raw stdout line onto q; _STDOUT_EOF on EOF.

    Lets recv() apply a timed get() so a wedged-but-alive subprocess (no
    output, process still running) is detected instead of blocking forever.
    """
    try:
        for line in stdout_pipe:
            q.put(line)
    except Exception as exc:  # pragma: no cover - defensive
        q.put(exc)
    finally:
        q.put(_STDOUT_EOF)

# E-polish outbound quote v3: teach cc the bridge-specific <quote> protocol.
# Injected once per session via --append-system-prompt so cc emits the tag
# at bubble-heads when it intends to quote-reply, and never as filler text.
QUOTE_SYSTEM_PROMPT = (
    "When you want to reply to a specific earlier message from the user, "
    "start that bubble with <quote>their exact words or a unique fragment"
    "</quote> followed by your reply. Example: if the user said "
    "'try it', a quote-reply bubble looks like "
    "<quote>try it</quote>ok trying now.\n\n"
    "Only use <quote> when you are actually quoting a prior user message. "
    "Never write the literal characters `<quote>` outside of an actual "
    "quote-reply."
)

# Outbound media: teach cc the bridge tag so split.py can dispatch real
# channel media messages. Only these four tags exist — no generic <media>.
MEDIA_SYSTEM_PROMPT = (
    "To send media (photo/gif/video/file), put a tag in your reply, one tag "
    'per file: <image path="/abs/p.jpg"> <gif path="/abs/a.gif"> '
    '<video path="/abs/v.mp4"> <file path="/abs/doc.pdf"> '
    "(file = pdf/txt/any other type). Tag position is bubble order: a "
    "reference/document the user asked for leads; a sticker or reaction "
    "image lands where it fits the conversation, not forced to the front. "
    "The bridge uploads it and delivers a real channel media message. "
    "The path must be a real existing local file - never fabricate.\n\n"
    "Stickers: The user loves it when you pair messages with stickers — "
    "they're your actions and inner voice. When you express affection, tease, "
    "react, or show a mood, call sticker(action='search', query=...) by vibe/emotion "
    "(e.g. '老婆别走' → search '爱你' '委屈' '哭') "
    "Don't wait for a special moment — weave them in naturally. "
    "Call sticker(action='pick', sticker_id=id), then send with the matching tag: "
    "<image path=\"...\"/> for .png/.jpg/.webp, <gif path=\"...\"/> for .gif. "
    "When user sends an image: caption '1' = save as sticker (code-routed), "
    "'0' = skip; no digit = if it looks sticker-worthy, just call "
    "sticker_admin(action='ingest') "
    "directly — dedup handles duplicates, so never ask, just try. "
    "Do not add daily photos. (never run /sticker-entry, CLI batch tool). "
    "Desc format for ingest: emotion/scene | image text | one-line visual (CN preferred)."
)

WX_ICLOUD_PROMPT = (
    "Large files are auto-routed to iCloud by the bridge — no action needed."
)

NIGHT_SYSTEM_PROMPT = (
    "Before 23:00, never proactively bring up sleep or push the user to go "
    "to bed. Bedtime coaxing starts only when a 23:00 nudge arrives."
)


class ClaudeCodeProvider(Provider):
    """Persistent `claude` CLI subprocess speaking stream-json over stdio."""

    def __init__(
        self,
        model: str | None = None,
        resume_sid: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        binary: str = "claude",
        effort_level: str | None = None,
        *,
        channel: str,
        stderr_log: Path | None = None,
        system_prompts: list[str] = (),
        marrow_bridge: bool = False,
        idle_soft_s: float = _DEFAULT_IDLE_SOFT_S,
        idle_hard_s: float = _DEFAULT_IDLE_HARD_S,
        turn_output_cap: int = _DEFAULT_TURN_OUTPUT_CAP,
    ) -> None:
        self.model = model
        self.resume_sid = resume_sid
        self.cwd = cwd
        self.extra_env = extra_env or {}
        self.binary = binary
        # /effort: when set, append `--effort <level>` so cc uses the matching
        # effort tier on this swap. None = omit; cc applies its own default.
        # Valid levels: low|medium|high|xhigh|max|ultracode|auto (cc 2.1.159+).
        self.effort_level = effort_level
        self.stderr_log = stderr_log
        self.system_prompts = list(system_prompts)
        self.marrow_bridge = marrow_bridge
        self.channel = channel
        self.idle_soft_s = idle_soft_s
        self.idle_hard_s = idle_hard_s
        self.turn_output_cap = turn_output_cap
        self.process: subprocess.Popen[str] | None = None
        self.alive: bool = False
        self.session_id: str | None = None
        self.usage_total: dict[str, int] = {}
        # Per-turn output cap state (reset at the start of every recv()).
        # turn_output_capped: sticky flag the loop layer reads after the turn
        # to send a "interrupted by the token cap" notice. NO retry on breach.
        # _turn_output_by_request: max output_tokens seen per request_id
        # within this turn; the current-turn total is the sum of these
        # values. Main-line only — subagent-attributed events are excluded.
        self.turn_output_capped: bool = False
        self._turn_output_by_request: dict[str, int] = {}
        # Populated in spawn(): stdout reader thread + its handoff queue.
        self._stdout_q: queue.Queue | None = None
        self._reader_thread: threading.Thread | None = None

    def _build_cmd(self) -> list[str]:
        cmd = [
            self.binary,
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
            # cc help mis-states this as "--print only" — in practice it works
            # with persistent stream-json subprocess too (claude-agent-sdk does
            # the same). Required to surface plaintext `thinking_delta` events;
            # without it, the final assistant `thinking` block is empty under
            # OAuth (redacted to signature only).
            "--include-partial-messages",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.resume_sid:
            cmd += ["--resume", self.resume_sid]
        if self.effort_level:
            cmd += ["--effort", self.effort_level]
        # Teach cc the bridge-specific <quote> + media-tag protocols once per
        # session. On --resume, cc replays prior turns so the appended prompt
        # persists; injecting per-turn would pollute context, hence here only.
        if self.system_prompts:
            cmd += [
                "--append-system-prompt",
                "\n\n".join(self.system_prompts),
            ]
        return cmd

    def spawn(self, env: dict[str, str] | None = None) -> None:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        merged.update(self.extra_env)
        if self.marrow_bridge:
            merged["MARROW_BRIDGE"] = "1"
        # Channel marker — the channel_marker hook reads this and prepends
        # the active channel to the user prompt.
        merged["MARROW_CHANNEL"] = self.channel
        try:
            self.process = subprocess.Popen(
                self._build_cmd(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.cwd,
                env=merged,
                start_new_session=True,
            )
        except OSError as e:
            raise ProviderSpawnError(f"claude spawn failed: {e}") from e
        self.alive = True
        # Non-blocking stdout consumption: a daemon reader pushes lines onto a
        # queue so recv() can time each get() and measure continuous silence.
        self._stdout_q = queue.Queue()
        self._reader_thread = threading.Thread(
            target=_read_stdout_to_queue,
            args=(self.process.stdout, self._stdout_q),
            name="cc-stdout-reader",
            daemon=True,
        )
        self._reader_thread.start()
        if self.stderr_log is not None:
            t = threading.Thread(
                target=_drain_stderr,
                args=(self.process.stderr, self.stderr_log),
                name="cc-stderr-drain",
                daemon=True,
            )
            t.start()

    def send(self, msg: str) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise ProviderDeadError("subprocess not alive")
        if self.process.stdin.closed:
            raise ProviderDeadError("stdin closed")
        payload = json.dumps(
            {"type": "user", "message": {"role": "user", "content": msg}}
        )
        try:
            self.process.stdin.write(payload + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise ProviderDeadError(f"stdin write failed: {e}") from e

    def send_raw_user_text(self, text: str) -> None:
        """E-polish /compact: pipe a literal user message (e.g. '/compact') to cc.

        cc parses leading slashes as native slash commands on its own side, so
        sending '/compact' through the standard user frame triggers cc's
        built-in compaction. Same wire format as `send`, named separately so
        callers can express the intent and tests can target it.
        """
        self.send(text)

    def _next_line(self) -> Any:
        """Block for the next stdout line, enforcing the idle liveness policy.

        Measures CONTINUOUS silence: the deadline is reset by the caller each
        time a line arrives (a fresh _next_line call starts a new clock).
        - At idle_soft_s of silence: poll the subprocess. Dead -> ProviderDead;
          alive -> keep waiting (self-heal window for a slow tool call).
        - At idle_hard_s of silence: process-group kill, raise ProviderStall.
        Returns the raw line, or _STDOUT_EOF on clean stream end.
        """
        assert self._stdout_q is not None
        start = time.monotonic()
        soft_checked = False
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= self.idle_hard_s:
                self._kill_process_group()
                raise ProviderStallError(
                    f"no stream event for {self.idle_hard_s:.0f}s (stall)"
                )
            # Wake at the next boundary (soft check, else hard deadline).
            if not soft_checked and elapsed < self.idle_soft_s:
                wait = self.idle_soft_s - elapsed
            else:
                wait = self.idle_hard_s - elapsed
            try:
                item = self._stdout_q.get(timeout=max(0.0, wait))
            except queue.Empty:
                if not soft_checked:
                    soft_checked = True
                    if self.process is not None and self.process.poll() is not None:
                        self.alive = False
                        raise ProviderDeadError(
                            "subprocess died during recv (soft check)"
                        )
                continue
            if isinstance(item, Exception):
                raise ProviderDeadError(f"stdout read error: {item}") from item
            return item

    def poll_line(self, timeout: float) -> Any:
        """Non-blocking-ish read of ONE raw stdout line for the idle listener.

        No liveness clock: idle silence between turns is normal and unbounded,
        so this never kills the process (unlike _next_line). Returns:
        - the raw line string when one is available within `timeout`,
        - None when the queue stayed empty (still idle),
        - POLL_EOF when the reader thread reported EOF or a read error; sets
          self.alive = False so the caller marks the provider dead.
        """
        if self._stdout_q is None:
            return POLL_EOF
        try:
            item = self._stdout_q.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if item is _STDOUT_EOF or isinstance(item, Exception):
            self.alive = False
            return POLL_EOF
        return item

    def recv(self, first_line: str | None = None) -> Iterator[dict[str, Any]]:
        if not self.alive or self.process is None or self._stdout_q is None:
            raise ProviderDeadError("subprocess not alive")
        # Reset per-turn output-cap state at the start of every send->result
        # cycle so the counter measures ONLY this turn's newly produced output.
        self.turn_output_capped = False
        self._turn_output_by_request = {}
        saw_result = False
        # first_line: a raw line already pulled off the queue by the idle
        # listener's poll_line. Process it before touching the queue so the
        # turn it opened is not lost; the idle liveness clock applies only from
        # the first real queue read onward, as for a plain recv().
        pending_first = first_line
        while True:
            if pending_first is not None:
                item: Any = pending_first
                pending_first = None
            else:
                item = self._next_line()
            if item is _STDOUT_EOF:
                break
            line = item.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("skip non-json line: %s (%s)", line[:120], e)
                continue
            t = ev.get("type")
            if t == "system" and ev.get("subtype") == "init":
                sid = ev.get("session_id")
                if isinstance(sid, str) and sid:
                    self.session_id = sid
            elif t == "assistant":
                usage = (ev.get("message") or {}).get("usage") or {}
                for k in _USAGE_KEYS:
                    v = usage.get(k)
                    if isinstance(v, int):
                        self.usage_total[k] = self.usage_total.get(k, 0) + v
            yield ev
            if t == "result":
                saw_result = True
                break
            # Output-cap brake: after yielding the event, tally this turn's
            # newly produced output tokens and interrupt if it exceeds the cap.
            # Done post-yield so the loop still gets the breaching event.
            # Subagent (Task-dispatched) events carry parent_tool_use_id and
            # are excluded — see _turn_output_breached.
            if t == "assistant" and self._turn_output_breached(ev):
                self.turn_output_capped = True
                log.warning(
                    "turn output cap %d exceeded (%d) — interrupting turn",
                    self.turn_output_cap,
                    sum(self._turn_output_by_request.values()),
                )
                self.cancel()
                return
        # A cap-interrupted turn returns cleanly above; any other missing
        # result is a genuine mid-turn death.
        if not saw_result:
            raise ProviderDeadError("subprocess died during recv")

    def _turn_output_breached(self, ev: dict[str, Any]) -> bool:
        """Accumulate this turn's OUTPUT tokens and report a cap breach.

        Counts ONLY usage.output_tokens (never input/cache_* — those reflect
        window size, easily 100k+, and would false-trigger every turn).
        Subagent output is excluded: a Task-dispatched subagent's assistant
        events are interleaved into the SAME stream but carry a top-level
        `parent_tool_use_id` (the dispatching tool_use id) instead of None —
        verified empirically against a real stream-json transcript (2.1.197,
        `claude --output-format stream-json` dispatching a Task). Counting
        those would false-trigger the cap on ordinary agent-dispatch turns.

        A turn spans several API requests (one per tool round trip); the
        stream also repeats identical usage lines within a request. Dedup by
        top-level `request_id` (snake_case — also verified on the same
        transcript; NOT `requestId`): keep the MAX output_tokens seen per
        request_id, then sum across requests. Returns True once the sum
        exceeds turn_output_cap.
        """
        cap = self.turn_output_cap
        if cap is None or cap <= 0:
            return False
        if ev.get("parent_tool_use_id") is not None:
            return False
        usage = (ev.get("message") or {}).get("usage") or {}
        out = usage.get("output_tokens")
        if not isinstance(out, int):
            return False
        # Events without a request_id (synthetic/test) each count once under a
        # unique key so they still sum rather than clobber one another.
        req_id = ev.get("request_id")
        key = req_id if isinstance(req_id, str) and req_id else f"_noid_{id(ev)}"
        prev = self._turn_output_by_request.get(key, 0)
        if out > prev:
            self._turn_output_by_request[key] = out
        return sum(self._turn_output_by_request.values()) > cap

    def _kill_process_group(self) -> None:
        """SIGKILL the whole process group (spawned with start_new_session).

        Used on hard stall: the subprocess is alive but wedged, so a plain
        terminate() may not reap child tool processes. Mirrors the intent of
        cancel()/close() but guarantees the group dies immediately.
        """
        self.alive = False
        p = self.process
        if p is None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                p.kill()
            except Exception:
                pass
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    def send_control_response(
        self, request_id: str, behavior: str, payload: dict[str, Any]
    ) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise ProviderDeadError("subprocess not alive")
        body = {"behavior": behavior, **payload}
        frame = {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": body,
            },
        }
        try:
            self.process.stdin.write(json.dumps(frame) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as e:
            raise ProviderDeadError(f"control_response write failed: {e}") from e

    def cancel(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            self.alive = False
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        self.alive = False

    def is_alive(self) -> bool:
        return (
            self.alive
            and self.process is not None
            and self.process.poll() is None
        )

    def close(self) -> None:
        # Grace budget tuned so cc's SessionEnd hook can finish writing the
        # lifecycle:end audit row + bridge_owns marker before SIGKILL fires.
        # archive_events on a long session can take >2s; SIGKILL'ing before
        # the INSERT lands produces silent_death alerts in marrow catchup.
        if self.process is None:
            self.alive = False
            return
        p = self.process
        try:
            if p.stdin and not p.stdin.closed:
                try:
                    p.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                p.wait(timeout=25)
            except subprocess.TimeoutExpired:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    try:
                        p.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            self.alive = False
