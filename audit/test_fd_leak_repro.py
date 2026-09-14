"""AUDIT DIAGNOSTIC (not production code).

Proves that the released ``test_no_descriptor_leak_over_many_connections``
passes only because CPython's cyclic collector happens to run during the
test.  With automatic collection disabled the same store calls leak two
file descriptors per call.

Run against the release worktree:

    PYTHONPATH=/home/user/yodaw-v1-audit \
      python -m pytest audit/test_fd_leak_repro.py -q
"""
from __future__ import annotations

import gc
import os

import pytest

from app.core.models import Mission
from app.storage.sqlite_store import MissionStore


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_with_sqlite_connection_does_not_close_the_connection(tmp_path):
    """``with sqlite3.connect(...) as db:`` manages the transaction only."""
    import sqlite3

    conn = sqlite3.connect(tmp_path / "x.sqlite")
    with conn as same:
        assert same is conn
    # still open and usable after the with-block
    assert conn.execute("select 1").fetchone() == (1,)
    conn.close()


def test_store_calls_leak_fds_when_gc_is_disabled(tmp_path):
    """Each MissionStore call opens a connection that is never closed."""
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        store = MissionStore(tmp_path / "fd.sqlite")
        store.list()
        baseline = _fd_count()

        for i in range(100):
            store.enqueue(Mission(goal=f"fd {i}", capability="repo-code"))
        after_enqueue = _fd_count()

        for i in range(100):
            store.get(f"__missing_{i}")
        after_get = _fd_count()

        reclaimed_before = _fd_count()
        gc.collect()
        reclaimed = reclaimed_before - _fd_count()
    finally:
        if gc_was_enabled:
            gc.enable()

    print(f"\nbaseline={baseline} after 100 enqueue={after_enqueue} "
          f"after 100 get={after_get} reclaimed by gc.collect()={reclaimed}")

    assert after_enqueue - baseline >= 100, (
        f"expected >=1 leaked fd per enqueue, got {after_enqueue - baseline}")
    assert reclaimed >= 100, (
        f"expected gc.collect() to reclaim the leaked handles, got {reclaimed}")


@pytest.mark.xfail(
    strict=True,
    reason="documents D-10: the shipped assertion (<25 fds over 150 store "
           "cycles) fails when this test runs in isolation, because the "
           "connections are only reclaimed by the cyclic collector. If the "
           "leak is fixed this XPASSes and the strict xfail fails - which is "
           "the intended signal.",
)
def test_shipped_regression_test_passes_only_because_gc_runs(tmp_path):
    """The released assertion (< 25 fds over 150 cycles) holds with GC on."""
    store = MissionStore(tmp_path / "fd2.sqlite")
    store.list()
    baseline = _fd_count()
    for i in range(150):
        mission = Mission(goal=f"fd {i}", capability="repo-code")
        store.enqueue(mission)
        claimed = store.claim_next("fd-coord")
        if claimed is not None:
            store.save(claimed)
        store.get(mission.id)
        store.list()
        store.status_counts()
    growth = _fd_count() - baseline
    print(f"\nwith GC enabled: fd growth over 150 cycles = {growth}")
    assert growth < 25, f"shipped assertion would fail: growth={growth}"
