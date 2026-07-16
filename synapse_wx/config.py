"""Minimal TOML config loader for synapse-wx."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "synapse-wx" / "config.toml"

# Default cwd cc subprocess spawns in. Aligns wx + cli project_slug so cli's
# native /resume picker sees wx sessions. Overridden by config.cc_cwd if set.
DEFAULT_CC_CWD = str(Path.home())


@dataclass
class Config:
    sessionend_command: str = "python -m marrow.sessionend_async --sid {sid}"
    poll_interval_sec: float = 1.0
    # Spacing between outbound reply bubbles. Wider gap avoids tripping the
    # iLink rate limit (ret=-2) on multi-bubble turns.
    bubble_gap_sec: float = 0.8
    # Outbound edge bubble cap: adjacent text bubbles are merged before the
    # send loop until the turn fits within this many bubbles. Main defense
    # against the iLink ~10-per-minute count quota (ret=-2).
    bubble_cap: int = 10
    # On a business rejection (ret!=0), wait this long for the quota window to
    # roll over, then retry the chunk once. Replaces the old exponential
    # backoff (useless against a minute-scale count quota).
    quota_wait_sec: float = 65.0
    target_wxid: str = ""
    marrow_repo_cmd: str = ""
    cc_cwd: str = ""  # cwd cc subprocess spawns in; empty = $HOME
    # Provider liveness: seconds of continuous stream silence before the soft
    # liveness check (poll process) and the hard idle kill (stall -> respawn).
    idle_soft_s: float = 60.0
    idle_hard_s: float = 300.0
    # Per-turn OUTPUT token brake: interrupt a runaway turn instead of burning
    # quota. 0 or negative disables.
    turn_output_cap: int = 20000
    # B1 sessions table. Empty = bridge runs without marrow session persistence
    # (no row written, /resume falls back to jsonl grep). Format strings get
    # {sid}, {model}, {channel} substituted.
    session_record_command: str = (
        "mw add-session --sid {sid} --model {model} --channel {channel} --effort {effort}"
    )
    session_get_model_command: str = "mw get-session-model --sid {sid}"
    # B6 recent-session picker for /resume (empty arg).
    session_list_recent_command: str = "mw list-recent-sessions --limit 10"
    # cwd resolver: prints the cwd for a sid, or empty line if unknown.
    session_cwd_command: str = "mw get-session-cwd --sid {sid}"
    # effort resolver: prints the stored effort for a sid, or empty on miss.
    session_get_effort_command: str = "mw get-session-effort --sid {sid}"
    # created_at resolver: prints ISO timestamp for a sid, or empty on miss.
    session_created_command: str = "mw get-session-created --sid {sid}"
    # B1: model /clear lands on (canonical id, "[1m]" suffix kept).
    clear_default_model: str = "claude-opus-4-6[1m]"
    # cc transcript dir for /resume jsonl fallback (and B7 history replay).
    cc_projects_dir: str = ""  # empty → ~/.claude/projects
    # B8: marrow.db path for the mm- / mm+ audit_log writer. Empty = bridge
    # runs without marrow audit integration (mm- / mm+ become silent no-ops on
    # the marrow side; the reply still goes to WeChat so the user knows it was
    # received).
    marrow_db_path: str = "~/.config/marrow/marrow.db"
    # PLAN 2c typing-event hunt: dump raw getupdates payloads until this local
    # date (inclusive, "YYYY-MM-DD"). Empty = off. Auto-expires after the date.
    raw_poll_log_until: str = ""

    # Outbox (cross-channel note delivery). Feature no-ops without target_wxid.
    # poll folds into MainLoop.tick; retry_max counts send_text CALLS (send_text
    # chunks + retries internally — no stacked retry on top).
    outbox_poll_interval_s: float = 5.0
    outbox_retry_max: int = 3

    # Ack string overrides from [ack_overrides] — key -> {style -> template}
    ack_overrides: dict | None = None


def load_config(path: Path | None = None) -> Config:
    """Load config.toml; return defaults if absent or malformed."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.is_file():
        return Config()
    try:
        raw = p.read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        logger.warning("config load failed (%s); using defaults", e)
        return Config()
    cfg = Config()
    session = data.get("session") or {}
    if isinstance(session, dict) and "sessionend_command" in session:
        val = session["sessionend_command"]
        if isinstance(val, str):
            cfg.sessionend_command = val
    loop = data.get("loop") or {}
    if isinstance(loop, dict) and "poll_interval_sec" in loop:
        val = loop["poll_interval_sec"]
        if isinstance(val, (int, float)) and val > 0:
            cfg.poll_interval_sec = float(val)
    if isinstance(loop, dict) and "bubble_gap_sec" in loop:
        val = loop["bubble_gap_sec"]
        if isinstance(val, (int, float)) and val >= 0:
            cfg.bubble_gap_sec = float(val)
    if isinstance(loop, dict) and "bubble_cap" in loop:
        val = loop["bubble_cap"]
        if isinstance(val, int) and val >= 1:
            cfg.bubble_cap = val
    send = data.get("send") or {}
    if isinstance(send, dict):
        if "quota_wait_sec" in send:
            val = send["quota_wait_sec"]
            if isinstance(val, (int, float)) and val >= 0:
                cfg.quota_wait_sec = float(val)
    user = data.get("user") or {}
    if isinstance(user, dict) and "target_wxid" in user:
        val = user["target_wxid"]
        if isinstance(val, str):
            cfg.target_wxid = val
    alerts = data.get("alerts") or {}
    if isinstance(alerts, dict) and "marrow_repo_cmd" in alerts:
        val = alerts["marrow_repo_cmd"]
        if isinstance(val, str):
            cfg.marrow_repo_cmd = val
    debug = data.get("debug") or {}
    if isinstance(debug, dict) and "raw_poll_log_until" in debug:
        val = debug["raw_poll_log_until"]
        if isinstance(val, str):
            cfg.raw_poll_log_until = val
    outbox = data.get("outbox") or {}
    if isinstance(outbox, dict):
        pi = outbox.get("poll_interval_s")
        if isinstance(pi, (int, float)) and not isinstance(pi, bool) and pi > 0:
            cfg.outbox_poll_interval_s = float(pi)
        rm = outbox.get("retry_max")
        if isinstance(rm, int) and not isinstance(rm, bool) and rm >= 1:
            cfg.outbox_retry_max = rm
    provider = data.get("provider") or {}
    if isinstance(provider, dict) and "cc_cwd" in provider:
        val = provider["cc_cwd"]
        if isinstance(val, str):
            cfg.cc_cwd = val
    if isinstance(provider, dict):
        soft = provider.get("idle_soft_s")
        if isinstance(soft, (int, float)) and not isinstance(soft, bool) and soft > 0:
            cfg.idle_soft_s = float(soft)
        hard = provider.get("idle_hard_s")
        if isinstance(hard, (int, float)) and not isinstance(hard, bool) and hard > 0:
            cfg.idle_hard_s = float(hard)
        cap = provider.get("turn_output_cap")
        if isinstance(cap, int) and not isinstance(cap, bool):
            cfg.turn_output_cap = cap
    if isinstance(session, dict):
        for field_name in (
            "session_record_command",
            "session_get_model_command",
            "session_list_recent_command",
            "session_cwd_command",
            "session_get_effort_command",
            "session_created_command",
            "clear_default_model",
            "cc_projects_dir",
            "marrow_db_path",
        ):
            if field_name in session:
                val = session[field_name]
                if isinstance(val, str):
                    setattr(cfg, field_name, val)
    ack = data.get("ack_overrides") or {}
    if isinstance(ack, dict):
        cfg.ack_overrides = {
            str(k): {str(s): str(t) for s, t in v.items() if isinstance(t, str)}
            for k, v in ack.items()
            if isinstance(v, dict)
        }

    return cfg
