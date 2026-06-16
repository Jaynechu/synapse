"""Shared session state for the bridge main loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BridgeState:
    """Single source of truth shared across loop / commands / sessionend.

    Loop owns mutation; commands and /info read. No lock here — Python attribute
    access is atomic and the only writer is the loop thread.
    """

    model: str | None = None
    session_id: str | None = None
    usage_total: dict[str, int] = field(default_factory=dict)
    # Snapshot of the most recent assistant turn's usage breakdown (overwrite,
    # not accumulate). Approximates current context size for /info:
    #   ctx ≈ input_tokens + cache_read_input_tokens + cache_creation_input_tokens
    # usage_total stays for cumulative cost reporting; do not use it as ctx.
    last_assistant_usage: dict[str, int] = field(default_factory=dict)
    rate_limit_info: dict[str, Any] | None = None
    last_user_msg_ts: float = 0.0
    last_result_ts: float = 0.0
    # E-polish: /thinking on|off — when True, the bridge collects cc
    # `thinking` content blocks and emits a single 【思考】 prefix bubble per
    # turn. Default off so nothing leaks to WeChat unless asked.
    thinking_on: bool = False
    # /quote on|off — when True, prepend a decorative ▎FRAGMENT bubble for
    # each <quote>...</quote> tag cc emits. Default off; the tag is always
    # stripped from the reply text regardless so the user never sees raw XML.
    quote_on: bool = False
    # /effort low|medium|high|xhigh|max|ultracode|auto → cc `--effort <level>`
    # on the next provider swap. Default "high" matches the WeChat default
    # (Opus 4.6 + high). The persisted bridge state file overlays whatever
    # the last session was using.
    effort_level: str = "high"
    # /voice cn|en — swaps the ack-string style. "cn" = 中文搞笑, "en" =
    # English short. Default cn matches Lumi's daily use. Persisted; survives
    # bridge crash. See commands.messages for the lookup table.
    voice_style: str = "cn"
    # In-memory picker arming. Set to "resume" right after /resume (empty arg)
    # renders the recent-session list; the next inbound dispatch consumes it
    # so a bare digit reply routes to the picker instead of leaking to cc as
    # prose. NOT persisted — a bridge crash drops the menu, user can /resume
    # again. Any inbound message clears it, the picker handler may re-arm.
    pending_picker: str | None = None
    # Snapshot of rows from the last /resume picker so a delayed digit reply
    # resolves against the SAME list the user saw, not a re-queried one.
    picker_rows: list[dict] = field(default_factory=list)
    # /cwd — current cwd cc subprocess spawns in. None = use DEFAULT_CC_CWD.
    # Persisted; survives bridge restart so the active project sticks.
    cc_cwd: str | None = None
