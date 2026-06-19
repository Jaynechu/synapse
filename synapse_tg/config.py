"""TOML config loader for synapse-tg."""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "synapse-tg" / "config.toml"


@dataclass
class TgConfig:
    bot_token: str = ""
    cc_path: str = "claude"
    data_dir: Path = field(default_factory=lambda: Path.home() / ".config" / "synapse-tg")
    marrow_bridge: bool = False
    cwd: Path | None = None
    default_model: str = "claude-opus-4-6[1m]"
    user_name: str = "user"
    assistant_name: str = "assistant"

    # Session lifecycle
    sessionend_command: str = ""
    cc_projects_dir: str = "~/.claude/projects"

    # Marrow integration (all empty = marrow disabled)
    marrow_db: str = "~/.config/marrow/marrow.db"
    session_record_command: str = ""
    session_get_model_command: str = ""
    session_cwd_command: str = ""
    session_get_effort_command: str = ""
    session_created_command: str = ""
    session_list_recent_command: str = ""

    # CWD presets
    cwd_presets: dict = field(default_factory=dict)

    # Ack string overrides from [ack_overrides] — key -> {style -> template}
    ack_overrides: dict = field(default_factory=dict)


def load_config(path: Path | None = None) -> TgConfig:
    """Load config.toml; return defaults if absent or malformed."""
    p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not p.is_file():
        return TgConfig()
    try:
        data = tomllib.loads(p.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        logger.warning("config load failed (%s); using defaults", e)
        return TgConfig()

    cfg = TgConfig()
    bot = data.get("bot") or {}
    if isinstance(bot, dict):
        if isinstance(bot.get("token"), str):
            cfg.bot_token = bot["token"]

    provider = data.get("provider") or {}
    if isinstance(provider, dict):
        if isinstance(provider.get("cc_path"), str):
            cfg.cc_path = provider["cc_path"]
        if isinstance(provider.get("cwd"), str):
            cfg.cwd = Path(provider["cwd"])
        if isinstance(provider.get("marrow_bridge"), bool):
            cfg.marrow_bridge = provider["marrow_bridge"]

    storage = data.get("storage") or {}
    if isinstance(storage, dict):
        if isinstance(storage.get("data_dir"), str):
            cfg.data_dir = Path(storage["data_dir"])

    provider_model = provider.get("default_model")
    if isinstance(provider_model, str) and provider_model:
        cfg.default_model = provider_model

    persona = data.get("persona") or {}
    if isinstance(persona, dict):
        if isinstance(persona.get("user_name"), str):
            cfg.user_name = persona["user_name"]
        if isinstance(persona.get("assistant_name"), str):
            cfg.assistant_name = persona["assistant_name"]

    marrow = data.get("marrow") or {}
    if isinstance(marrow, dict):
        if isinstance(marrow.get("db"), str):
            cfg.marrow_db = marrow["db"]
        if isinstance(marrow.get("sessionend_command"), str):
            cfg.sessionend_command = marrow["sessionend_command"]
        if isinstance(marrow.get("session_record_command"), str):
            cfg.session_record_command = marrow["session_record_command"]
        if isinstance(marrow.get("session_get_model_command"), str):
            cfg.session_get_model_command = marrow["session_get_model_command"]
        if isinstance(marrow.get("session_cwd_command"), str):
            cfg.session_cwd_command = marrow["session_cwd_command"]
        if isinstance(marrow.get("session_get_effort_command"), str):
            cfg.session_get_effort_command = marrow["session_get_effort_command"]
        if isinstance(marrow.get("session_created_command"), str):
            cfg.session_created_command = marrow["session_created_command"]
        if isinstance(marrow.get("session_list_recent_command"), str):
            cfg.session_list_recent_command = marrow["session_list_recent_command"]

    if isinstance(provider.get("cc_projects_dir"), str):
        cfg.cc_projects_dir = provider["cc_projects_dir"]

    presets = data.get("cwd_presets") or {}
    if isinstance(presets, dict):
        cfg.cwd_presets = {str(k): str(v) for k, v in presets.items() if isinstance(v, str)}

    ack = data.get("ack_overrides") or {}
    if isinstance(ack, dict):
        cfg.ack_overrides = {
            str(k): {str(s): str(t) for s, t in v.items() if isinstance(t, str)}
            for k, v in ack.items()
            if isinstance(v, dict)
        }

    return cfg
