"""UserPromptSubmit hook: tag every prompt with the current channel.

Always emits `[channel: <name>]` so Stellan knows which surface Lumi is on.
Default channel is `cli` when `MARROW_CHANNEL` is unset (bridges set their
own value before spawning cc; the cli has no env, so it falls back to cli).

When the same sid was last seen on a different channel, emits
`[channel: <current> <- <previous>]` so a cross-channel switch is visible
on the very next prompt. The detection reads `last_active.json` (the
shared cross-channel pointer) and matches sid; no time gate, since the
sid match itself is the strong signal.

The hook also writes `last_active.json` on each prompt so the next channel
that picks up this sid can detect the switch in turn. Bridge loops also
write at turn-end; the writes are idempotent and atomic, so the redundancy
is harmless.

Install via ~/.claude/settings.json:

    {
      "hooks": {
        "UserPromptSubmit": [
          {"hooks": [{"type": "command",
                       "command": "python -m synapse_wx.hooks.channel_marker"}]}
        ]
      }
    }
"""

from __future__ import annotations

import json
import sys
import time
from os import environ
from pathlib import Path

try:  # last_active is part of synapse_wx; import guarded so the hook
    # still injects a basic marker even if the module fails to load.
    from synapse_core import last_active as _last_active
except Exception:  # pragma: no cover - defensive
    _last_active = None

# Mirrors synapse_wx/__main__.py LAST_ACTIVE_PATH — kept as a separate
# constant since the hook runs standalone and must not import __main__.
LAST_ACTIVE_PATH = Path.home() / ".config" / "marrow" / "last_active.json"


def _read_payload() -> dict:
    """Parse the cc hook JSON payload from stdin. Returns {} on any error."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _previous_channel(sid: str, reader=None) -> str:
    """Return the previously-recorded channel for `sid`, or empty string.

    `reader` is the last_active module (injected for tests). When None,
    falls back to the package-level import; returns "" if that failed.
    """
    if not sid:
        return ""
    mod = reader if reader is not None else _last_active
    if mod is None:
        return ""
    try:
        data = mod.read(LAST_ACTIVE_PATH)
    except Exception:
        return ""
    if not data or data.get("sid") != sid:
        return ""
    return (data.get("channel") or "").strip()


def build_output(
    env: dict | None = None,
    payload: dict | None = None,
    reader=None,
) -> dict:
    """Return the hook output dict. Always emits a channel marker."""
    src = env if env is not None else environ
    current = (src.get("MARROW_CHANNEL") or "").strip() or "cli"
    sid = ((payload or {}).get("session_id") or "").strip()
    prev = _previous_channel(sid, reader=reader)
    if prev and prev != current:
        # Cross-channel resume: same sid, different channel. A bare
        # `[channel: ...]` tag tends to get treated as background noise —
        # promote to an actionable sentence so cc actually adjusts.
        ctx = (
            f"[channel: {current} <- {prev}] CROSS-CHANNEL CONTINUATION: "
            f"User just switched platform but still same session. "
        )
    else:
        ctx = f"[channel: {current}]"
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }
    }


def _stamp_last_active(sid: str, channel: str, writer=None) -> None:
    """Best-effort write of the cross-channel pointer. Never raises."""
    mod = writer if writer is not None else _last_active
    if mod is None or not sid:
        return
    try:
        mod.write(LAST_ACTIVE_PATH, sid, channel, time.time())
    except Exception:
        pass


def main() -> int:
    payload = _read_payload()
    # Subagent (Task tool dispatch) — no channel marker; subagents do
    # not switch surfaces and the marker just adds noise.
    tpath = (payload.get("transcript_path") or "")
    if "/tasks/" in tpath:
        return 0
    out = build_output(payload=payload)
    sid = (payload.get("session_id") or "").strip()
    current = (environ.get("MARROW_CHANNEL") or "").strip() or "cli"
    _stamp_last_active(sid, current)
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
