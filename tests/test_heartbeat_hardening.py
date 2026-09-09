"""
Stage 8.5 heartbeat hardening (post-live-smoke defect).

Mandated coverage for the heartbeat defect found during the
Stage 8 live smoke:

A. a long-blocked worker still receives advancing heartbeats
   on the persisted payload (what the API and the watchdog read)
B. the watchdog never recovers a healthy long-running mission
C. the watchdog recovers a genuinely stale mission
D. heartbeat persistence works under concurrent SQLite activity
E. heartbeat-loop failures are counted and logged, never silent,
   and the loop keeps running
F. shutdown stops heartbeat activity cleanly

All tests are hermetic: real SQLite, real threads, fake workers.
No Ollama.
"""

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from app.core.models import Mission, MissionStatus
from app.runtime import coordinator as coordinator_module
from app.runtime.coordinator import Coordinator
from app.runtime.repo_leases import RepoLeaseManager
from app.storage.sqlite_store import MissionStore
from app.workers.registry import registry


class BlockingWorker:
    """Fake worker that blocks until an event is set."""

    name = "hb-blocking-bud"

    def __init__(self, capability: str, release: threading.Event):
        self.capabilities = {capability}
        self.release = release

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def health(self):
        return {"status": "READY"}

    def execute(self, goal, metadata=None):
        self.release.wait(timeout=30)
        return {
            "success": True,
            "output": {"goal": goal},
            "evidence": [],
            "error": None,
        }


@pytest.fixture
def fast_heartbeat(monkeypatch):
    """1s heartbeat interval, 6s staleness for fast hermetic tests."""
    monkeypatch.setattr(coordinator_module, "heartbeat_seconds", lambda: 1)
    monkeypatch.setattr(
        coordinator_module, "stale_after_seconds", lambda: 6
    )


def _column_heartbeat(store: MissionStore, mission_id: str):
    db = sqlite3.connect(str(store.path))
    try:
        row = db.execute(
            "SELECT heartbeat_at FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
    finally:
        db.close()
    return row[0] if row else None


def _payload_heartbeat(store: MissionStore, mission_id: str):
    db = sqlite3.connect(str(store.path))
    try:
        row = db.execute(
            "SELECT json_extract(payload, '$.heartbeat_at') "
            "FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
    finally:
        db.close()
    return row[0] if row else None


def test_heartbeat_advances_while_worker_blocks(tmp_path, fast_heartbeat):
    """(A) Blocked worker does not freeze the persisted heartbeat."""
    store = MissionStore(tmp_path / "db.sqlite")
    release = threading.Event()
    registry.workers.append(BlockingWorker("hb-block-a", release))

    mission = Mission(goal="blocked", capability="hb-block-a")
    store.enqueue(mission)

    coord = Coordinator(store=store)
    coord.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = store.get(mission.id)
            if current.status == MissionStatus.running:
                break
            time.sleep(0.05)

        assert current.status == MissionStatus.running

        hb1 = store.get(mission.id).heartbeat_at
        time.sleep(2.5)
        hb2 = store.get(mission.id).heartbeat_at

        assert hb1 and hb2
        assert hb2 > hb1, "heartbeat must advance while the worker blocks"

        # The payload (what the API and the watchdog read) and the
        # runtime column must agree after a heartbeat.
        assert _payload_heartbeat(store, mission.id) == _column_heartbeat(
            store, mission.id
        )
    finally:
        release.set()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if store.get(mission.id).status == MissionStatus.passed:
                break
            time.sleep(0.05)
        coord.stop(drain=False)


def test_watchdog_never_recovers_healthy_long_running_mission(
    tmp_path, fast_heartbeat
):
    """(B) Fresh heartbeats protect a long-running mission."""
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)
    release = threading.Event()
    registry.workers.append(BlockingWorker("hb-block-b", release))

    mission = Mission(goal="healthy long mission", capability="hb-block-b")
    store.enqueue(mission)

    runner = Coordinator(store=store, leases=leases)
    runner.start()

    watchdog = Coordinator(
        store=store, leases=leases, id_prefix="watchdog"
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if store.get(mission.id).status == MissionStatus.running:
                break
            time.sleep(0.05)

        assert store.get(mission.id).status == MissionStatus.running

        # Longer than the 6s staleness cutoff: a frozen heartbeat
        # WOULD be recovered here, fresh ones must not be.
        time.sleep(7)

        assert watchdog.recover_stale_missions() == []
        current = store.get(mission.id)
        assert current.status == MissionStatus.running
        assert current.claimed_by == runner.id
    finally:
        release.set()
        runner.stop(drain=True, timeout=15)


def test_watchdog_recovers_genuinely_stale_mission(tmp_path, fast_heartbeat):
    """(C) A mission whose heartbeats stopped is recovered, never re-run."""
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)

    mission = Mission(goal="orphaned", capability="repo-code")
    store.enqueue(mission)

    claimed = store.claim_next("dead-coordinator")

    # Simulate a coordinator that died right after claiming: its
    # last heartbeat is hours old.
    claimed.heartbeat_at = "2026-09-09T00:00:00+00:00"
    store.save(claimed)

    fresh = Coordinator(store=store, leases=leases, id_prefix="fresh")
    recovered = fresh.recover_stale_missions()

    assert recovered == [claimed.id]

    final = store.get(claimed.id)
    assert final.status == MissionStatus.failed
    assert final.result["error"]["type"] == "InterruptedExecution"
    assert final.result["error"]["recovered_by"] == fresh.id

    events = [e["event_type"] for e in store.events(claimed.id)]
    assert events.count("mission.recovered") == 1


def test_heartbeat_persists_under_concurrent_sqlite_activity(tmp_path):
    """(D) Heartbeats survive concurrent readers/writers."""
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(goal="contended", capability="repo-code")
    store.enqueue(mission)
    claimed = store.claim_next("coord-contended")

    noise_errors: list[str] = []

    def noise():
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                store.record_event(
                    claimed.id, "noise.event", data={"n": 1}
                )
                store.list()
        except Exception as exc:  # pragma: no cover
            noise_errors.append(repr(exc))

    threads = [threading.Thread(target=noise) for _ in range(4)]
    for thread in threads:
        thread.start()

    beats = 0
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        assert store.heartbeat(claimed.id, "coord-contended") is True
        beats += 1
        time.sleep(0.05)

    for thread in threads:
        thread.join()

    assert noise_errors == []
    assert beats >= 5

    # Every heartbeat landed in both the column and the payload.
    assert _payload_heartbeat(store, claimed.id) == _column_heartbeat(
        store, claimed.id
    )


def test_heartbeat_failure_is_counted_and_logged_not_silent(
    tmp_path, fast_heartbeat, caplog
):
    """(E) Heartbeat failures are surfaced and the loop survives."""
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(goal="hb failure", capability="repo-code")
    store.enqueue(mission)
    claimed = store.claim_next("coord-hb-fail")

    coord = Coordinator(store=store, id_prefix="hb-fail")

    def failing_heartbeat(mission_id, coordinator_id):
        raise RuntimeError("simulated store outage")

    coord._inflight.add(claimed.id)

    with caplog.at_level(logging.ERROR, logger="yodaw.coordinator"):
        from unittest.mock import patch

        with patch.object(store, "heartbeat", failing_heartbeat):
            coord.start()
            time.sleep(1.2)
            coord.stop(drain=False)

    assert coord._hb_failures >= 1, "failures must be counted"
    assert coord._hb_beats >= 2, "loop must survive failures"
    assert "heartbeat persist failed" in caplog.text
    assert "simulated store outage" in caplog.text
    assert coord.stats()["heartbeat_failures"] == coord._hb_failures


def test_shutdown_stops_heartbeat_activity(tmp_path, fast_heartbeat):
    """(F) After stop(), no further heartbeats are persisted."""
    store = MissionStore(tmp_path / "db.sqlite")
    release = threading.Event()
    registry.workers.append(BlockingWorker("hb-block-f", release))

    mission = Mission(goal="shutdown", capability="hb-block-f")
    store.enqueue(mission)

    coord = Coordinator(store=store)
    coord.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if store.get(mission.id).status == MissionStatus.running:
            break
        time.sleep(0.05)

    time.sleep(1.5)
    hb_before = _column_heartbeat(store, mission.id)

    coord.stop(drain=False)

    assert not (coord._hb_thread and coord._hb_thread.is_alive())
    assert coord.stats()["heartbeat_thread_alive"] is False

    time.sleep(1.5)
    hb_after = _column_heartbeat(store, mission.id)
    assert hb_after == hb_before, "no heartbeats may persist after stop"

    release.set()
