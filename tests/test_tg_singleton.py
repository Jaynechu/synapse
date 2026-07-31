"""Boot singleton lock: held flock replaces the old pidfile-kill dance.

A second acquire on a live lock must fail without disturbing the holder; a
lock whose holder already exited (fd closed, as on process death) must be
reclaimable by the next acquire.
"""

from __future__ import annotations

import os

from synapse_tg.__main__ import _acquire_singleton_lock


def test_second_acquire_fails_while_held(tmp_path):
    lock_path = tmp_path / "synapse-tg.lock"

    fd1 = _acquire_singleton_lock(lock_path)
    assert fd1 >= 0

    fd2 = _acquire_singleton_lock(lock_path)
    assert fd2 == -1

    fd3 = _acquire_singleton_lock(lock_path)
    assert fd3 == -1

    os.close(fd1)


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path):
    lock_path = tmp_path / "synapse-tg.lock"

    fd1 = _acquire_singleton_lock(lock_path)
    assert fd1 >= 0
    os.close(fd1)

    fd2 = _acquire_singleton_lock(lock_path)
    assert fd2 >= 0

    os.close(fd2)
