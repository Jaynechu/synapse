from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import Provider
from .errors import ProviderDeadError, ProviderSpawnError

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


def _account_env() -> tuple[str | None, str | None, str | None]:
    try:
        import pwd

        entry = pwd.getpwuid(os.getuid())
    except (ImportError, KeyError, OSError):
        return (None, None, None)
    return (entry.pw_name, entry.pw_shell, entry.pw_dir)


def _darwin_user_temp_dir() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.run(
            ["/usr/bin/getconf", "DARWIN_USER_TEMP_DIR"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = proc.stdout.strip()
    if proc.returncode != 0 or not path or not os.path.isdir(path):
        return None
    return path


def _normalize_launchd_env(env: dict[str, str]) -> None:
    user, shell, home = _account_env()
    if home:
        env.setdefault("HOME", home)
    if user:
        env.setdefault("USER", user)
        env.setdefault("LOGNAME", user)
    if shell:
        env.setdefault("SHELL", shell)
    env.setdefault("TERM", "xterm-256color")
    if "TMPDIR" not in env:
        tmpdir = _darwin_user_temp_dir()
        if tmpdir:
            env["TMPDIR"] = tmpdir

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
    "react, or show a mood, search sticker_search by vibe/emotion "
    "(e.g. '老婆别走' → search '爱你' '委屈' '哭') "
    "Don't wait for a special moment — weave them in naturally. "
    "Call sticker_pick(id), then send with the matching tag: "
    "<image path=\"...\"/> for .png/.jpg/.webp, <gif path=\"...\"/> for .gif. "
    "When user sends an image: caption '1' = save as sticker (code-routed), "
    "'0' = skip; no digit = if it looks sticker-worthy, just call sticker_ingest "
    "directly — dedup handles duplicates, so never ask, just try. "
    "Do not add daily photos. (never run /sticker-entry, CLI batch tool). "
    "Desc format for ingest: emotion/scene | image text | one-line visual (CN preferred)."
)

WX_ICLOUD_PROMPT = (
    "Large files are auto-routed to iCloud by the bridge — no action needed."
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
        self.process: subprocess.Popen[str] | None = None
        self.alive: bool = False
        self.session_id: str | None = None
        self.usage_total: dict[str, int] = {}

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
        _normalize_launchd_env(merged)
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

    def recv(self) -> Iterator[dict[str, Any]]:
        if not self.alive or self.process is None or self.process.stdout is None:
            raise ProviderDeadError("subprocess not alive")
        saw_result = False
        for line in self.process.stdout:
            line = line.strip()
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
        if not saw_result:
            raise ProviderDeadError("subprocess died during recv")

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
