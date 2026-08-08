"""Kick: wake cortex from a bridge.

Shared by both bridges (tg/wx) — channel-agnostic. Kick = a detached,
fire-and-forget subprocess in the cortex venv (bridges cannot import cortex).
The command lives in synapse [outbox].kick_cmd; absent = the feature no-ops
with a one-time warning. Never blocks the bridge.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess

logger = logging.getLogger(__name__)

_warned_no_cmd = False


def _kick_argv(kick_cmd) -> list[str] | None:
    if not kick_cmd:
        return None
    if isinstance(kick_cmd, (list, tuple)):
        argv = [str(x) for x in kick_cmd if str(x).strip()]
    else:
        argv = shlex.split(str(kick_cmd))
    return argv or None


def kick(kick_cmd, kind: str, *, note_id=None, minutes=None, text=None,
         text_chars=None) -> bool:
    """Spawn one detached cortex.kick. Returns True if launched. Absent kick_cmd
    warns once, then no-ops. Never raises. `text` is truncated to `text_chars`
    and passed as --text so the wakeup note carries the triggering context."""
    global _warned_no_cmd
    argv = _kick_argv(kick_cmd)
    if argv is None:
        if not _warned_no_cmd:
            logger.warning(
                "cortex_kick: [outbox].kick_cmd not set — kick disabled")
            _warned_no_cmd = True
        return False
    argv = argv + ["--kind", str(kind)]
    if note_id is not None:
        argv += ["--note-id", str(note_id)]
    if minutes is not None:
        argv += ["--minutes", str(minutes)]
    if text:
        t = str(text)
        if text_chars and text_chars > 0:
            t = t[:int(text_chars)]
        argv += ["--text", t]
    try:
        subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, env={**os.environ},
        )
        return True
    except OSError as e:
        logger.warning("cortex_kick: spawn failed (%s)", e)
        return False
