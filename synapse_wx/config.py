"""WeChat view over the shared defaults table (synapse_core/config.py)."""

from __future__ import annotations

from pathlib import Path

from synapse_core.config import USER_CONFIG_PATHS, ConfigView

DEFAULT_CONFIG_PATH = USER_CONFIG_PATHS["wx"]


class Config(ConfigView):
    CHANNEL = "wx"

    def cc_cwd_path(self) -> str:
        """cwd the cc subprocess spawns in, user-expanded."""
        return str(Path(self.cc_cwd).expanduser())


def load_config(path: Path | None = None) -> Config:
    """Defaults table with ~/.config/synapse-wx/config.toml merged over it."""
    return Config(Path(path) if path is not None else DEFAULT_CONFIG_PATH)
