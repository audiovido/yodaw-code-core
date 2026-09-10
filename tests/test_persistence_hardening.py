"""
Stage 8.9: persistence hardening.

- WAL journal mode + busy timeout on every connection
- in-place migration of a legacy Stage 7 database
- mission_events table for observability
- concurrent readers/writers with WAL never deadlock the queue
"""

import json
import sqlite3
import threading
import time

from app.core.models import Mission
from app.storage.db import connect
from app.storage.sqlite_store import MissionStore, _migrate


def test_connections_use_wal_and_busy_timeout(tmp_path):
    db_path = tmp_path / "wal.sqlite"

    # Instantiate the store so migrations create the schema.
    MissionStore(db_path)

    db = connect(db_path)

    try:
        assert db.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0].lower() == "wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    finally:
        db.close()


def test_fresh_database_gets_full_schema(tmp_path):
    db_path = tmp_path / "fresh.sqlite"

    MissionStore(db_path)

    db = sqlite3.connect(db_path)

    try:
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(missions)").fetchall()
        }

        required = {
            "id",
            "payload",
            "status",
            "goal",
            "repo_key",
            "claimed_by",
            "claimed_at",
            "heartbeat_at",
            "cancel_requested",
            "created_at",
            "updated_at",
        }

        assert required <= columns

        indexes = {
            row[1]
            for row in db.execute(
                "PRAGMA index_list(missions)"
            ).fetchall()
        }

        assert "idx_missions_status_created" in indexes
        assert "idx_missions_repo_status" in indexes

        version = db.execute("PRAGMA user_version").fetchone()[0]

        assert version >= 1
    finally:
        db.close()


def test_legacy_stage7_database_migrates_in_place(tmp_path):
    """
    A Stage 7 database is just (id, payload) rows. Stage 8 must
    upgrade it without losing a single mission record.
    """
    db_path = tmp_path / "legacy.sqlite"

    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE missions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )

    payloads = []

    for i in range(5):
        mission = {
            "id": f"m_legacy{i}",
            "goal": f"legacy goal {i}",
            "capability": "repo-code",
            "status": "PASS" if i % 2 else "FAIL",
            "worker": "repo-code-bud",
            "result": {"commit_sha": f"sha{i}"},
            "evidence": [],
            "metadata": {"repo_path": f"/old/{i}"},
            "created_at": "2026-09-09T12:00:00+00:00",
            "updated_at": "2026-09-09T12:00:00+00:00",
        }
        payloads.append(mission)
        legacy.execute(
            "INSERT INTO missions(id, payload) VALUES(?, ?)",
            (mission["id"], json.dumps(mission)),
        )

    legacy.commit()
    legacy.close()

    # Opening with the Stage 8 store migrates in place.
    store = MissionStore(db_path)

    missions = store.list()

    assert len(missions) == 5

    for original in payloads:
        fetched = store.get(original["id"])

        assert fetched is not None
        assert fetched.goal == original["goal"]
        assert fetched.status.value == original["status"]
        assert (
            fetched.result.get("commit_sha")
            == original["result"]["commit_sha"]
        )

    # Structural checks after migration.
    db = sqlite3.connect(db_path)

    try:
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(missions)").fetchall()
        }

        assert "claimed_by" in columns
        assert "heartbeat_at" in columns

        rows = db.execute(
            "SELECT goal, status FROM missions WHERE id='m_legacy0'"
        ).fetchone()

        assert rows == ("legacy goal 0", "FAIL")

        version = db.execute("PRAGMA user_version").fetchone()[0]

        assert version >= 1
    finally:
        db.close()

    # Migrated missions are terminal; the same goal can be
    # enqueued fresh without duplicate rejection.
    store.enqueue(
        Mission(
            goal="legacy goal 0",
            capability="repo-code",
            metadata={"repo_path": "/old/0"},
        )
    )


def test_mission_events_persist_across_reopen(tmp_path):
    db_path = tmp_path / "events.sqlite"
    store = MissionStore(db_path)

    mission = Mission(goal="g", capability="repo-code")
    store.enqueue(mission)
    store.record_event(mission.id, "custom.event", attempt=2, data={"k": "v"})

    reopened = MissionStore(db_path)
    events = reopened.events(mission.id)

    types = [e["event_type"] for e in events]

    assert "mission.queued" in types
    assert "custom.event" in types

    custom = next(e for e in events if e["event_type"] == "custom.event")

    assert custom["attempt"] == 2
    assert custom["data"] == {"k": "v"}


def test_concurrent_readers_and_writers_under_wal(tmp_path):
    """
    Multiple threads hammering enqueue/claim/save/get/list/events
    against one SQLite file must complete without deadlock or
    corruption and never double-claim a mission.
    """
    db_path = tmp_path / "concurrent.sqlite"
    store = MissionStore(db_path)

    EXPECTED_MISSIONS = 4 * 4
    errors = []
    claimed = []
    lock = threading.Lock()
    writers_finished = 0
    writers_done = threading.Event()

    def writer(i):
        nonlocal writers_finished
        try:
            for j in range(4):
                store.enqueue(
                    Mission(
                        goal=f"w{i} goal {j}",
                        capability="repo-code",
                        metadata={"repo_path": f"/repo/w{i}j{j}"},
                    )
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            with lock:
                writers_finished += 1
                if writers_finished == 4:
                    writers_done.set()

    def claimer():
        try:
            # Claim until every enqueued mission is drained; the
            # monotonic deadline only fires if claiming stalls.
            deadline = time.monotonic() + 30
            while True:
                with lock:
                    done = len(claimed)
                if writers_done.is_set() and done >= EXPECTED_MISSIONS:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"claimer timed out with {done}/{EXPECTED_MISSIONS} claimed"
                    )
                mission = store.claim_next(f"coord-{threading.get_ident()}")

                if mission:
                    with lock:
                        claimed.append(mission.id)
                    store.heartbeat(mission.id, f"coord-{threading.get_ident()}")
                    from app.core.models import MissionStatus

                    mission.status = MissionStatus.passed
                    store.save(mission)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            for _ in range(50):
                store.list()
                store.status_counts()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = (
        [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        + [threading.Thread(target=claimer) for _ in range(3)]
        + [threading.Thread(target=reader) for _ in range(2)]
    )

    for t in threads:
        t.start()

    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert not any(t.is_alive() for t in threads), "worker thread deadlocked"
    assert len(claimed) == EXPECTED_MISSIONS, (
        f"expected {EXPECTED_MISSIONS} claimed, got {len(claimed)}"
    )
    assert len(set(claimed)) == EXPECTED_MISSIONS, "mission double-claimed"
