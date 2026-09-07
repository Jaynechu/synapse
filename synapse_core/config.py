"""One loader for every channel: packaged defaults + the channel's user file.

`config.default.toml` next to this module is the single defaults table. A user
file is deep-merged over it, so a user file only ever carries differences.
`FIELDS` maps the attribute name the code reads to the (section, key) it lives
under and the channels that consume it — no value is written down here.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tomllib
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULTS_PATH = Path(__file__).with_name("config.default.toml")

CHANNELS = ("tg", "wx")

USER_CONFIG_PATHS = {
    "tg": Path.home() / ".config" / "synapse-tg" / "config.toml",
    "wx": Path.home() / ".config" / "synapse-wx" / "config.toml",
}

BOTH = CHANNELS
TG = ("tg",)
WX = ("wx",)

# attribute -> (section, key, channels)
FIELDS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "timezone": ("core", "timezone", BOTH),
    "cc_path": ("provider", "cc_path", BOTH),
    "cwd": ("provider", "cwd", TG),
    "cc_cwd": ("provider", "cc_cwd", WX),
    "marrow_bridge": ("provider", "marrow_bridge", TG),
    "cc_projects_dir": ("provider", "cc_projects_dir", BOTH),
    "default_model": ("provider", "default_model", BOTH),
    "clear_default_model": ("provider", "clear_default_model", WX),
    "idle_soft_s": ("provider", "idle_soft_s", BOTH),
    "idle_hard_s": ("provider", "idle_hard_s", BOTH),
    "turn_output_cap": ("provider", "turn_output_cap", BOTH),
    "unsolicited_storm_cap": ("provider", "unsolicited_storm_cap", BOTH),
    "max_consecutive_deaths": ("provider", "max_consecutive_deaths", TG),
    "data_dir": ("storage", "data_dir", TG),
    "log_file": ("storage", "log_file", TG),
    "user_name": ("persona", "user_name", TG),
    "assistant_name": ("persona", "assistant_name", TG),
    "marrow_db": ("marrow", "db", BOTH),
    "session_record_command": ("marrow", "session_record_command", BOTH),
    "session_get_model_command": ("marrow", "session_get_model_command", BOTH),
    "session_cwd_command": ("marrow", "session_cwd_command", BOTH),
    "session_get_effort_command": ("marrow", "session_get_effort_command", BOTH),
    "session_created_command": ("marrow", "session_created_command", BOTH),
    "session_list_recent_command": ("marrow", "session_list_recent_command", BOTH),
    "send_retry_max": ("send", "send_retry_max", TG),
    "retry_after_cap_sec": ("send", "retry_after_cap_sec", TG),
    "http_connect_timeout_s": ("send", "http_connect_timeout_s", TG),
    "http_read_timeout_s": ("send", "http_read_timeout_s", TG),
    "http_write_timeout_s": ("send", "http_write_timeout_s", TG),
    "http_pool_timeout_s": ("send", "http_pool_timeout_s", TG),
    "quota_wait_sec": ("send", "quota_wait_sec", WX),
    "bot_token": ("bot", "token", TG),
    "chat_id": ("tg", "chat_id", TG),
    "allowed_user_ids": ("tg", "allowed_user_ids", TG),
    "shell_enabled": ("cortex", "shell_enabled", TG),
    "shell_id": ("cortex", "shell_id", TG),
    "shell_state_dir": ("cortex", "shell_state_dir", TG),
    "shell_socket": ("cortex", "shell_socket", TG),
    "shell_peer_fallback": ("cortex", "shell_peer_fallback", TG),
    "shell_idle_min": ("cortex", "shell_idle_min", TG),
    "shell_note_render_cmd": ("cortex", "note_render_cmd", TG),
    "shell_note_render_timeout_s": ("cortex", "note_render_timeout_s", TG),
    "shell_note_render_alert_after": ("cortex", "note_render_alert_after", TG),
    "shell_note_tag": ("cortex", "shell_note_tag", TG),
    "shell_fuse_tokens": ("cortex", "fuse_tokens", TG),
    "shell_fuse_tag": ("cortex", "fuse_tag", TG),
    "shell_fuse_prompt_text": ("cortex", "fuse_prompt_text", TG),
    "shell_context_notify": ("cortex", "context_notify", TG),
    "shell_context_notify_start": ("cortex", "context_notify_start", TG),
    "shell_context_notify_step": ("cortex", "context_notify_step", TG),
    "target_wxid": ("user", "target_wxid", WX),
    "poll_interval_sec": ("loop", "poll_interval_sec", WX),
    "bubble_gap_sec": ("loop", "bubble_gap_sec", WX),
    "bubble_cap": ("loop", "bubble_cap", WX),
    "icloud_outbox": ("media", "icloud_outbox", WX),
    "marrow_repo_cmd": ("alerts", "marrow_repo_cmd", WX),
    "raw_poll_log_until": ("debug", "raw_poll_log_until", WX),
    "cwd_presets": ("cwd_presets", "", BOTH),
    "ack_overrides": ("ack_overrides", "", BOTH),
}

# Sections/keys renamed when the defaults table was unified. A user file still
# carrying the old spelling is migrated in memory with one warning per load.
RENAMES: dict[tuple[str, str], tuple[str, str]] = {
    ("session", "marrow_db_path"): ("marrow", "db"),
    ("session", "session_record_command"): ("marrow", "session_record_command"),
    ("session", "session_get_model_command"): ("marrow", "session_get_model_command"),
    ("session", "session_cwd_command"): ("marrow", "session_cwd_command"),
    ("session", "session_get_effort_command"): ("marrow", "session_get_effort_command"),
    ("session", "session_created_command"): ("marrow", "session_created_command"),
    ("session", "session_list_recent_command"): ("marrow", "session_list_recent_command"),
    ("session", "cc_projects_dir"): ("provider", "cc_projects_dir"),
    ("session", "clear_default_model"): ("provider", "clear_default_model"),
}

# Validation floors for numeric keys (minimum, exclusive?). A user value below
# the floor is rejected with a warning and the table value stands. These are
# constraints, not defaults.
_MIN: dict[str, tuple[float, bool]] = {
    "idle_soft_s": (0, True),
    "idle_hard_s": (0, True),
    "unsolicited_storm_cap": (0, False),
    "max_consecutive_deaths": (1, False),
    "poll_interval_sec": (0, True),
    "bubble_gap_sec": (0, False),
    "bubble_cap": (1, False),
    "quota_wait_sec": (0, False),
    "retry_after_cap_sec": (0, True),
    "http_connect_timeout_s": (0, True),
    "http_read_timeout_s": (0, True),
    "http_write_timeout_s": (0, True),
    "http_pool_timeout_s": (0, True),
    "shell_idle_min": (0, True),
    "shell_note_render_timeout_s": (0, True),
    "shell_note_render_alert_after": (0, False),
    "shell_fuse_tokens": (0, False),
    "shell_context_notify_start": (0, False),
    "shell_context_notify_step": (0, False),
}

# Attributes whose runtime type TOML cannot express. "" means "unset".
_PATH_FIELDS = frozenset({"data_dir"})
_OPTIONAL_PATH_FIELDS = frozenset({"cwd"})
_OPTIONAL_INT_FIELDS = frozenset({"chat_id"})


class ConfigError(Exception):
    """A config value could not be resolved from any owning table."""


def defaults() -> dict[str, Any]:
    """The packaged defaults table as a nested dict (fresh copy each call)."""
    return tomllib.loads(DEFAULTS_PATH.read_bytes().decode("utf-8"))


def default_value(attr: str) -> Any:
    """One attribute's packaged default, coerced the same way a load coerces."""
    section, key, _ = FIELDS[attr]
    raw = defaults()[section]
    return _coerce(attr, raw if not key else raw[key])


def _coerce(attr: str, value: Any) -> Any:
    if attr in _PATH_FIELDS:
        return Path(str(value)).expanduser()
    if attr in _OPTIONAL_PATH_FIELDS:
        text = str(value).strip()
        return Path(text).expanduser() if text else None
    if attr in _OPTIONAL_INT_FIELDS:
        return int(value) if value else None
    return value


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge `over` onto a copy of `base`. Tables merge, scalars and
    arrays replace."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate(data: dict) -> dict:
    """Rewrite renamed sections/keys in a user file, warning once per rename."""
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in data.items()}
    for (old_section, old_key), (new_section, new_key) in RENAMES.items():
        section = out.get(old_section)
        if not isinstance(section, dict) or old_key not in section:
            continue
        logger.warning(
            "config: [%s].%s is renamed to [%s].%s — update the user file",
            old_section, old_key, new_section, new_key,
        )
        out.setdefault(new_section, {})
        if isinstance(out[new_section], dict):
            out[new_section].setdefault(new_key, section.pop(old_key))
        else:
            section.pop(old_key)
    out.pop("session", None)
    return out


def read_user_file(path: Path) -> dict:
    """A user config file as a nested dict. Absent or malformed -> empty."""
    if not path.is_file():
        return {}
    try:
        return _migrate(tomllib.loads(path.read_bytes().decode("utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        logger.warning("config load failed (%s); using defaults", e)
        return {}


def _accept(attr: str, default: Any, value: Any) -> tuple[bool, Any]:
    """Validate one user value against the table entry it overrides."""
    if isinstance(default, bool):
        return (isinstance(value, bool), value)
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return (False, value)
        floor = _MIN.get(attr)
        if floor is not None and (value <= floor[0] if floor[1] else value < floor[0]):
            return (False, value)
        return (True, type(default)(value))
    if isinstance(default, str):
        if not isinstance(value, str):
            return (False, value)
        # An empty override on a key that ships a real value means "unset".
        return (bool(value.strip()) or not default.strip(), value)
    if isinstance(default, list):
        return (isinstance(value, list), value)
    if isinstance(default, dict):
        return (isinstance(value, dict), value)
    return (True, value)


def resolve(channel: str, *, path: Path | None = None,
            overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], set[str]]:
    """Merge a user file over the defaults table.

    `path=None` means no user file at all — the packaged table only. The
    channel's own loader passes USER_CONFIG_PATHS[channel] explicitly, so
    constructing a view never touches the machine's live config by accident.

    Returns the attribute -> value mapping for every field the channel reads,
    plus the set of attributes the user file (or `overrides`) set explicitly.
    """
    if channel not in CHANNELS:
        raise ConfigError(f"unknown channel {channel!r}")
    base = defaults()
    user = read_user_file(Path(path)) if path is not None else {}
    merged = _merge(base, user)

    values: dict[str, Any] = {}
    explicit: set[str] = set()
    for attr, (section, key, channels) in FIELDS.items():
        if channel not in channels:
            continue
        block = base[section]
        default = block if not key else block[key]
        merged_block = merged.get(section, {})
        raw = merged_block if not key else merged_block.get(key, default)
        ok, raw = _accept(attr, default, raw)
        if not ok:
            logger.warning("config: [%s].%s rejected (%r) — keeping default",
                           section, key or "*", raw)
            raw = default
        elif _user_set(user, section, key):
            explicit.add(attr)
        values[attr] = _coerce(attr, raw)

    for attr, value in (overrides or {}).items():
        if attr not in FIELDS:
            raise TypeError(f"unknown config field {attr!r}")
        values[attr] = value
        explicit.add(attr)
    return values, explicit


def _user_set(user: dict, section: str, key: str) -> bool:
    block = user.get(section)
    if not isinstance(block, dict):
        return False
    return bool(block) if not key else key in block


class ConfigView:
    """Attribute view over one resolved mapping. No field defaults of its own:
    every value came from the table or the user file."""

    CHANNEL = ""

    def __init__(self, path: Path | None = None, /, **overrides: Any) -> None:
        values, explicit = resolve(self.CHANNEL, path=path, overrides=overrides)
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "_explicit", explicit)

    def __getattr__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, "_values")[name]
        except KeyError:
            raise AttributeError(
                f"{type(self).__name__} has no config field {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._values:
            self._values[name] = value
            self._explicit.add(name)
        else:
            object.__setattr__(self, name, value)

    def is_explicit(self, name: str) -> bool:
        """Did the user file (or a constructor override) set this key?"""
        return name in self._explicit

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._values!r})"


def _dump(data: dict[str, Any]) -> str:
    return "\n".join(f"{k} = {v!r}" for k, v in sorted(data.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m synapse_core.config",
                                 description="Inspect the synapse defaults table.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--defaults", action="store_true",
                   help="print the packaged defaults table verbatim")
    g.add_argument("--resolved", action="store_true",
                   help="print defaults + the channel user file, as the code reads it")
    ap.add_argument("--channel", choices=CHANNELS, help="channel for --resolved")
    ap.add_argument("--config", type=Path, help="user file to use instead of the default path")
    args = ap.parse_args(argv)

    if args.defaults:
        sys.stdout.write(DEFAULTS_PATH.read_bytes().decode("utf-8"))
        return 0
    if not args.channel:
        ap.error("--resolved needs --channel")
    user_path = args.config or USER_CONFIG_PATHS[args.channel]
    values, explicit = resolve(args.channel, path=user_path)
    sys.stdout.write(f"# channel={args.channel} user={user_path}\n")
    for k, v in sorted(values.items()):
        mark = "  # user" if k in explicit else ""
        sys.stdout.write(f"{k} = {v!r}{mark}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
