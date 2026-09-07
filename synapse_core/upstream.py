"""Resolved config of the repos that OWN values this bridge only consumes.

marrow owns [cortex.breaker]; cortex owns the free-round cadence. Both expose a
`config --resolved` CLI that already merges their own packaged defaults with
their user file, so this bridge reads the owner instead of keeping a copy.

Both lookups are cached for the life of the process — a bridge restart is what
picks up an upstream config change.
"""

from __future__ import annotations

import logging
import subprocess
import tomllib
from typing import Any

logger = logging.getLogger(__name__)

MARROW_CLI = ("mw", "config", "--resolved")
CORTEX_MODULE = "cortex.ctl"
TIMEOUT_SEC = 15.0

_cache: dict[str, dict[str, Any] | None] = {}


class UpstreamError(Exception):
    """An owning repo's config could not be read."""


def _run(argv: tuple[str, ...] | list[str], cwd: str | None = None) -> dict[str, Any]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=TIMEOUT_SEC, cwd=cwd, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise UpstreamError(f"{argv[0]} not runnable: {e}") from e
    if proc.returncode != 0:
        raise UpstreamError(f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    try:
        return tomllib.loads(proc.stdout)
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise UpstreamError(f"{argv[0]} output is not TOML: {e}") from e


def marrow_config() -> dict[str, Any]:
    """marrow's fully resolved config. Raises UpstreamError if unreadable."""
    if "marrow" not in _cache:
        try:
            _cache["marrow"] = _run(MARROW_CLI)
        except UpstreamError as e:
            logger.warning("marrow config unreadable: %s", e)
            _cache["marrow"] = None
    data = _cache["marrow"]
    if data is None:
        raise UpstreamError("marrow config unreadable")
    return data


def cortex_config() -> dict[str, Any]:
    """cortex's fully resolved config, run in the interpreter and repo root
    marrow's [cortex] names. Raises UpstreamError if unreadable."""
    if "cortex" not in _cache:
        try:
            block = marrow_config().get("cortex") or {}
            python = str(block.get("venv_python") or "").strip()
            root = str(block.get("repo_root") or "").strip()
            if not python or not root:
                raise UpstreamError("marrow [cortex].venv_python/repo_root unset")
            _cache["cortex"] = _run([python, "-m", CORTEX_MODULE, "config", "--resolved"],
                                    cwd=root)
        except UpstreamError as e:
            logger.warning("cortex config unreadable: %s", e)
            _cache["cortex"] = None
    data = _cache["cortex"]
    if data is None:
        raise UpstreamError("cortex config unreadable")
    return data


def value(config: dict[str, Any], dotted: str) -> Any:
    """Nested lookup, e.g. value(cortex_config(), "wake.default_sleep_min")."""
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise UpstreamError(f"key {dotted!r} missing")
        node = node[part]
    return node


def reset_cache() -> None:
    _cache.clear()
