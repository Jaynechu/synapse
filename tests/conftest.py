"""Repo-wide safety net: block real `claude` process spawns during tests.

`synapse_core.providers.cc.ClaudeCodeProvider.spawn()` is the only provider
that shells out via `subprocess.Popen`. Scoped to cc.py's own `subprocess`
binding (not the global module) so unrelated Popen/run callers (marrow_session
mw lookups, alerts, cortex_kick) keep working untouched.
"""

from __future__ import annotations

import subprocess
import types

import pytest

import synapse_core.providers.cc as cc


def _blocked_popen(*args: object, **kwargs: object) -> None:
    raise RuntimeError(f"real process spawn blocked in tests: Popen(args={args!r})")


@pytest.fixture(autouse=True)
def _block_real_process_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    guarded = types.SimpleNamespace(**vars(subprocess))
    guarded.Popen = _blocked_popen
    monkeypatch.setattr(cc, "subprocess", guarded)
