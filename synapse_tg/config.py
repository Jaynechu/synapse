"""Telegram view over the shared defaults table (synapse_core/config.py)."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from synapse_core import upstream
from synapse_core.config import USER_CONFIG_PATHS, ConfigView

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = USER_CONFIG_PATHS["tg"]

# cortex owns the free-round cadence; [cortex].shell_idle_min in a user file
# pins it, otherwise it is read from here.
CORTEX_IDLE_KEY = "wake.default_sleep_min"


class TgConfig(ConfigView):
    CHANNEL = "tg"

    def shell_socket_path(self) -> Path:
        return Path(self.shell_socket).expanduser()

    def marrow_config_dir(self) -> Path:
        """The shared marrow config/state dir (parent of marrow.db). Home of
        config.toml, breaker.json and fuse_events.json — the cross-repo
        protocol files marrow, cortex and this bridge all read."""
        return Path(self.marrow_db).expanduser().parent

    def idle_window_min(self) -> float:
        """Minutes of user silence before one rendered note turn is fed in.

        Owned by cortex ([wake].default_sleep_min) so both shells run the same
        free-round cadence. An explicit [cortex].shell_idle_min in the user
        file wins; an unreachable cortex falls back to the defaults table."""
        if not self.is_explicit("shell_idle_min"):
            try:
                value = upstream.value(upstream.cortex_config(), CORTEX_IDLE_KEY)
            except upstream.UpstreamError as e:
                logger.warning("cortex %s unreadable (%s) — using the packaged "
                               "fallback %s", CORTEX_IDLE_KEY, e, self.shell_idle_min)
            else:
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    return float(value)
                logger.warning("cortex %s is %r — using the packaged fallback %s",
                               CORTEX_IDLE_KEY, value, self.shell_idle_min)
        return float(self.shell_idle_min)

    def _cortex_shells(self) -> list[str]:
        """marrow's [cortex].shells, lowercased (single source, resolved via
        marrow_config_dir — same file cortex's shell_enabled() reads).
        Missing/unreadable marrow config or missing key -> empty list."""
        p = self.marrow_config_dir() / "config.toml"
        try:
            if p.is_file():
                data = tomllib.loads(p.read_bytes().decode("utf-8"))
                shells = (data.get("cortex") or {}).get("shells")
                if isinstance(shells, list):
                    return [str(s).strip().lower() for s in shells]
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError) as e:
            logger.warning("marrow config read failed (%s) — shell off", e)
        return []

    def shell_active(self) -> bool:
        """shell_enabled AND `shell_id` listed in marrow's [cortex].shells.
        Unresolvable -> shell off (standalone-synapse fallback)."""
        if not self.shell_enabled:
            return False
        return self.shell_id.strip().lower() in self._cortex_shells()

    def shell_peer(self) -> str:
        """The other cortex shell a transfer from this one lands on. Falls
        back to shell_peer_fallback when [cortex].shells names no other."""
        me = self.shell_id.strip().lower()
        for s in self._cortex_shells():
            if s != me:
                return s
        return self.shell_peer_fallback

    def effective_allowed_user_ids(self) -> list[int]:
        """Whitelist actually enforced: allowed_user_ids if set, else
        [chat_id] if set (private chats: chat_id == user_id), else empty
        (accept-all)."""
        if self.allowed_user_ids:
            return [x for x in self.allowed_user_ids
                    if isinstance(x, int) and not isinstance(x, bool)]
        if self.chat_id is not None:
            return [self.chat_id]
        return []


def load_config(path: Path | None = None) -> TgConfig:
    """Defaults table with ~/.config/synapse-tg/config.toml merged over it."""
    return TgConfig(Path(path) if path is not None else DEFAULT_CONFIG_PATH)
