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

    # Provider liveness: seconds of continuous stream silence before the soft
    # liveness check (poll process) and the hard idle kill (stall -> respawn).
    idle_soft_s: float = 60.0
    idle_hard_s: float = 300.0
    # Per-turn OUTPUT token brake: interrupt a runaway turn instead of burning
    # quota. 0 or negative disables.
    turn_output_cap: int = 20000
    user_name: str = "user"
    assistant_name: str = "assistant"

    # Session lifecycle
    cc_projects_dir: str = "~/.claude/projects"

    # Marrow integration (all empty = marrow disabled)
    marrow_db: str = "~/.config/marrow/marrow.db"
    session_record_command: str = ""
    session_get_model_command: str = ""
    session_cwd_command: str = ""
    session_get_effort_command: str = ""
    session_created_command: str = ""
    session_list_recent_command: str = ""

    # Outbound send resilience
    send_retry_max: int = 2
    retry_after_cap_sec: float = 60.0

    # HTTPX transport timeouts (seconds) for the PTB Bot. get_updates uses a
    # separate request whose read timeout must stay above the long-poll timeout.
    http_connect_timeout_s: float = 10.0
    http_read_timeout_s: float = 30.0
    http_write_timeout_s: float = 30.0
    http_pool_timeout_s: float = 10.0

    # Outbox (cross-channel note delivery). Feature no-ops without chat_id.
    chat_id: int | None = None
    outbox_poll_interval_s: float = 5.0
    outbox_retry_max: int = 3

    # Watch + kick (P6). kick_cmd = cortex.kick launcher (venv python + module),
    # e.g. ["/path/.venv/bin/python", "-m", "cortex.kick"]. Empty = watch/kick off.
    # Morning flag-pull reads the cortex night flag + morning_start.
    outbox_kick_cmd: list = field(default_factory=list)
    outbox_kick_text_chars: int = 200
    outbox_receipt_text_chars: int = 120
    outbox_kick_media_placeholder: str = "[media]"
    # Marks a delivered note as bridge-sent (vs the resident session's own
    # chat), so her phone can tell them apart at a glance. Empty disables.
    outbox_note_prefix: str = "\U0001f4ee "
    cortex_wake_state_file: str = ""
    night_morning_start: str = "06:00"
    timezone: str = "Australia/Melbourne"

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

    tg = data.get("tg") or {}
    if isinstance(tg, dict):
        cid = tg.get("chat_id")
        if isinstance(cid, int) and not isinstance(cid, bool):
            cfg.chat_id = cid

    outbox = data.get("outbox") or {}
    if isinstance(outbox, dict):
        pi = outbox.get("poll_interval_s")
        if isinstance(pi, (int, float)) and not isinstance(pi, bool) and pi > 0:
            cfg.outbox_poll_interval_s = float(pi)
        rm = outbox.get("retry_max")
        if isinstance(rm, int) and not isinstance(rm, bool) and rm >= 1:
            cfg.outbox_retry_max = rm
        kc = outbox.get("kick_cmd")
        if isinstance(kc, list):
            cfg.outbox_kick_cmd = [str(x) for x in kc]
        elif isinstance(kc, str) and kc.strip():
            cfg.outbox_kick_cmd = kc
        ktc = outbox.get("kick_text_chars")
        if isinstance(ktc, int) and not isinstance(ktc, bool) and ktc > 0:
            cfg.outbox_kick_text_chars = ktc
        rtc = outbox.get("receipt_text_chars")
        if isinstance(rtc, int) and not isinstance(rtc, bool) and rtc > 0:
            cfg.outbox_receipt_text_chars = rtc
        kmp = outbox.get("kick_media_placeholder")
        if isinstance(kmp, str) and kmp.strip():
            cfg.outbox_kick_media_placeholder = kmp
        if "note_prefix" in outbox and isinstance(outbox["note_prefix"], str):
            cfg.outbox_note_prefix = outbox["note_prefix"]

    cortex = data.get("cortex") or {}
    if isinstance(cortex, dict):
        ws = cortex.get("wake_state_file")
        if isinstance(ws, str):
            cfg.cortex_wake_state_file = ws
        ms = cortex.get("morning_start")
        if isinstance(ms, str) and ms.strip():
            cfg.night_morning_start = ms

    core = data.get("core") or {}
    if isinstance(core, dict) and isinstance(core.get("timezone"), str):
        cfg.timezone = core["timezone"]

    provider = data.get("provider") or {}
    if isinstance(provider, dict):
        if isinstance(provider.get("cc_path"), str):
            cfg.cc_path = provider["cc_path"]
        if isinstance(provider.get("cwd"), str):
            cfg.cwd = Path(provider["cwd"])
        if isinstance(provider.get("marrow_bridge"), bool):
            cfg.marrow_bridge = provider["marrow_bridge"]
        soft = provider.get("idle_soft_s")
        if isinstance(soft, (int, float)) and not isinstance(soft, bool) and soft > 0:
            cfg.idle_soft_s = float(soft)
        hard = provider.get("idle_hard_s")
        if isinstance(hard, (int, float)) and not isinstance(hard, bool) and hard > 0:
            cfg.idle_hard_s = float(hard)
        cap = provider.get("turn_output_cap")
        if isinstance(cap, int) and not isinstance(cap, bool):
            cfg.turn_output_cap = cap

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

    send = data.get("send") or {}
    if isinstance(send, dict):
        if isinstance(send.get("send_retry_max"), int) and not isinstance(send.get("send_retry_max"), bool):
            cfg.send_retry_max = send["send_retry_max"]
        if isinstance(send.get("retry_after_cap_sec"), (int, float)) and not isinstance(send.get("retry_after_cap_sec"), bool):
            cfg.retry_after_cap_sec = float(send["retry_after_cap_sec"])
        for key, attr in (
            ("http_connect_timeout_s", "http_connect_timeout_s"),
            ("http_read_timeout_s", "http_read_timeout_s"),
            ("http_write_timeout_s", "http_write_timeout_s"),
            ("http_pool_timeout_s", "http_pool_timeout_s"),
        ):
            v = send.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                setattr(cfg, attr, float(v))

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
