#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


_SYNAPSE_HOOKS: dict[str, dict[str, list[dict[str, str]]]] = {
    "wx": {
        "UserPromptSubmit": [
            {"matcher": "", "command": "{venv} -m synapse_wx.hooks.channel_marker"},
        ],
    },
}


def _is_synapse_hook(cmd: str) -> bool:
    """Return true for Synapse hook commands."""
    return "synapse_wx.hooks" in cmd or "synapse_tg.hooks" in cmd


def _hook_command(template: str, venv_python: str) -> str:
    """Replace hook command placeholders."""
    return template.replace("{venv}", venv_python)


def register_hooks(channel: str, venv_python: str, settings_path: str) -> bool:
    """Register Synapse hooks for a channel."""
    settings_file = Path(settings_path).expanduser()
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    if settings_file.exists():
        try:
            settings: dict = json.loads(settings_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"error: settings.json is invalid JSON: {settings_file}", file=sys.stderr)
            return False
    else:
        settings = {}

    hooks: dict = settings.setdefault("hooks", {})
    channel_hooks = _SYNAPSE_HOOKS.get(channel, {})

    for event, entries in channel_hooks.items():
        event_list: list = hooks.setdefault(event, [])

        for entry in entries:
            new_cmd = _hook_command(entry["command"], venv_python)
            matcher = entry["matcher"]
            group = next((g for g in event_list if g.get("matcher") == matcher), None)

            if group is None:
                group = {"matcher": matcher, "hooks": []}
                event_list.append(group)

            group_hooks: list = group.setdefault("hooks", [])
            group_hooks[:] = [
                h for h in group_hooks
                if not _is_synapse_hook(h.get("command", ""))
            ]
            group_hooks.append({"type": "command", "command": new_cmd})

    settings_file.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"[{channel}] hooks registered in {settings_file}")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(
            "usage: register_hooks.py <channel> <bridge_home> <settings_path>",
            file=sys.stderr,
        )
        raise SystemExit(2)

    channel_arg, bridge_home_arg, settings_path_arg = sys.argv[1:]
    bridge_home = Path(bridge_home_arg).expanduser().resolve()
    venv_python_arg = str(bridge_home / ".venv" / "bin" / "python")
    raise SystemExit(0 if register_hooks(channel_arg, venv_python_arg, settings_path_arg) else 1)
