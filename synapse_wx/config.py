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
    target_wxid: str = ""
    marrow_repo_cmd: str = ""
    cc_cwd: str = ""  # cwd cc subprocess spawns in; empty = $HOME
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
    provider = data.get("provider") or {}
    if isinstance(provider, dict) and "cc_cwd" in provider:
        val = provider["cc_cwd"]
        if isinstance(val, str):
            cfg.cc_cwd = val
    if isinstance(session, dict):
        for field_name in (
            "session_record_command",
            "session_get_model_command",
            "session_list_recent_command",
            "session_cwd_command",
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
