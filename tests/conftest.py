"""Repo-wide safety net: block real `claude` process spawns during tests, block
any write to the LIVE marrow config dir, and stub the upstream (marrow/cortex)
`config --resolved` subprocesses.

`synapse_core.providers.cc.ClaudeCodeProvider.spawn()` is the only provider
that shells out via `subprocess.Popen`. Scoped to cc.py's own `subprocess`
binding (not the global module) so unrelated Popen/run callers (marrow_session
mw lookups, alerts) keep working untouched.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

import synapse_core.breaker as breaker
import synapse_core.providers.cc as cc
import synapse_core.upstream as upstream

# What the owning repos answer during tests. Values are fixtures, not defaults:
# the real ones come from `mw config --resolved` / `cortex.ctl config
# --resolved`, which must never be spawned from the suite.
UPSTREAM_MARROW = {
    "cortex": {
        "breaker": {
            "enabled": True,
            "fuse_threshold": 2,
            "window_hours": 24,
            "trip_message": (
                "Circuit breaker tripped: fuse #{count} within {hours}h. Cortex "
                "autonomous activity paused ({scope}). Clear with ct-duty cli|tg|all."
            ),
        },
    },
}
UPSTREAM_CORTEX = {"wake": {"default_sleep_min": 55}}


@pytest.fixture(autouse=True)
def _stub_upstream_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may shell out to marrow or cortex. Tests needing the failure
    path monkeypatch these back to raisers."""
    upstream.reset_cache()
    monkeypatch.setattr(upstream, "marrow_config", lambda: UPSTREAM_MARROW)
    monkeypatch.setattr(upstream, "cortex_config", lambda: UPSTREAM_CORTEX)


def _blocked_popen(*args: object, **kwargs: object) -> None:
    raise RuntimeError(f"real process spawn blocked in tests: Popen(args={args!r})")


@pytest.fixture(autouse=True)
def _block_real_process_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    guarded = types.SimpleNamespace(**vars(subprocess))
    guarded.Popen = _blocked_popen
    monkeypatch.setattr(cc, "subprocess", guarded)


# The real runtime dir. A test that resolves breaker.json / fuse_events.json
# here would tally against the machine's live fuse history — and could trip the
# real circuit breaker mid-suite. Fail loudly instead.
_LIVE_CONFIG_DIR = (Path.home() / ".config" / "marrow").resolve()


def _under_live_dir(p) -> bool:
    try:
        rp = Path(p).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return rp == _LIVE_CONFIG_DIR or _LIVE_CONFIG_DIR in rp.parents


@pytest.fixture(autouse=True)
def _no_live_breaker_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every breaker path a test resolves must be under tmp_path. A config
    whose marrow_db still points at the live dir is an isolation bug."""
    def _guard(orig, name):
        def wrapper(config_dir, *a, **kw):
            out = orig(config_dir, *a, **kw)
            if _under_live_dir(out):
                raise AssertionError(
                    f"test isolation: breaker.{name}() resolved to the LIVE dir "
                    f"{out!r} — set marrow_db to a tmp_path file so no test "
                    f"touches real ~/.config/marrow/ runtime")
            return out
        return wrapper
    for name in ("breaker_path", "fuse_path"):
        monkeypatch.setattr(breaker, name, _guard(getattr(breaker, name), name))
