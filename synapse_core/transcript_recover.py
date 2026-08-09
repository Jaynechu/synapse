"""Recover the last assistant reply from a claude-code session transcript.

Given the provider's working directory, session id, and the timestamp when the
turn was sent, this module scans the JSONL transcript for assistant message
entries that arrived at or after that time and returns the combined text.

Used by boot recovery to detect turns that the provider completed but the
bridge never delivered (crash between _stream_response and _deliver_reply).

Never raises — any parse or IO error returns None so callers can treat it as
"nothing recoverable".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from synapse_core.text_clean import strip_tool_xml

logger = logging.getLogger(__name__)

# Extra seconds subtracted from since_ts to absorb small clock skew between
# the bridge clock and the cc process clock.
_CLOCK_SLOP_S = 5.0


def _munged_cwd(cwd: str) -> str:
    """Mirror the munging cc applies to a project path for its projects dir:
    replace every '/' and '.' with '-'.  Leading '-' from a root '/' is kept
    (cc does the same)."""
    return cwd.replace("/", "-").replace(".", "-")


def _transcript_path(cc_projects_dir: str, cwd: str, session_id: str) -> Path:
    """Resolve ~/.claude/projects/<munged-cwd>/<session_id>.jsonl."""
    munged = _munged_cwd(cwd)
    return Path(cc_projects_dir).expanduser() / munged / f"{session_id}.jsonl"


def recover_reply(
    cc_projects_dir: str,
    cwd: str,
    session_id: str,
    since_ts: float,
) -> str | None:
    """Return assistant text from the transcript at or after since_ts, or None.

    Parses the JSONL transcript, collects text blocks from assistant messages
    whose ISO timestamp is >= (since_ts - _CLOCK_SLOP_S), applies strip_tool_xml,
    joins with "\\n\\n" (same as _collect_turn), and returns the result.

    Returns None when:
    - the transcript file is missing
    - no qualifying assistant messages are found
    - any unrecoverable parse error occurs
    """
    try:
        path = _transcript_path(cc_projects_dir, cwd, session_id)
        if not path.is_file():
            logger.debug("transcript_recover: file not found: %s", path)
            return None

        cutoff = since_ts - _CLOCK_SLOP_S
        chunks: list[str] = []

        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") != "assistant":
                    continue
                # Extract timestamp — cc writes ISO 8601 in the "timestamp" field.
                ts_str = entry.get("timestamp")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(
                            ts_str.replace("Z", "+00:00")
                        ).replace(tzinfo=timezone.utc).timestamp()
                    except (ValueError, TypeError):
                        ts = None
                else:
                    ts = None

                if ts is not None and ts < cutoff:
                    continue

                # Collect text blocks from this assistant message.
                msg = entry.get("message") or {}
                for block in msg.get("content", []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "text":
                        continue
                    raw_text = block.get("text", "")
                    if not isinstance(raw_text, str):
                        continue
                    cleaned = strip_tool_xml(raw_text)
                    if cleaned:
                        chunks.append(cleaned)

        if not chunks:
            return None
        return "\n\n".join(chunks)

    except Exception as e:  # noqa: BLE001 — never raise from recovery
        logger.warning("transcript_recover: error reading transcript: %s", e)
        return None
