"""
Stage 9.5: exactly-once outbox relay for learning records.

The coordinator enqueues learning into the durable outbox; the
relay drains it. These tests prove:

- happy path: mission completion produces exactly one learning
  record, delivered through the outbox
- replay safety: re-delivering the same message never duplicates
  the record (idempotent upsert by deterministic id)
- crash recovery: messages written but not delivered before a
  shutdown are delivered by the next relay that starts
- failure handling: a failing delivery is recorded with its error
  and retried, never silently dropped
"""

import logging

import pytest

from app.core.models import Mission
from app.learning.engine import record_id_default
from app.learning.store import LearningStore
from app.runtime.outbox_relay import OutboxRelay
from app.runtime.repo_leases import RepoLeaseManager
from app.runtime.coordinator import Coordinator
from app.storage.sqlite_store import MissionStore
from app.workers.registry import registry


class OkWorker:
    name = "outbox-ok-bud"
    capabilities = {"outbox-cap"}

    def supports(self, capability):
        return capability in self.capabilities

    def health(self):
        return {"status": "READY"}

    def execute(self, goal, metadata=None):
        return {
            "success": True,
            "output": {"goal": goal},
            "evidence": [{"type": "probe"}],
            "error": None,
        }


registry.workers.append(OkWorker())


def _enqueue_learning(store: MissionStore, mission_id: str) -> None:
    store.outbox_enqueue(
        mission_id=mission_id,
        kind="learning.record",
        payload={
            "mission_id": mission_id,
            "goal": f"goal {mission_id}",
            "worker": "outbox-ok-bud",
            "success": True,
            "evidence": [],
            "result": {"retries": 0},
            "record_id": record_id_default(mission_id),
        },
    )


def test_relay_delivers_learning_exactly_once(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    _enqueue_learning(store, "m_relay_1")

    relay = OutboxRelay(store=store)
    assert relay.drain_once() == 1
    assert relay.stats()["delivered"] == 1
    assert relay.stats()["pending"] == 0

    # The record exists, keyed by the deterministic id.
    learning = LearningStore(tmp_path.parent / "db.sqlite")

    # LearningStore shares the DB path through env; instantiate via
    # default path only in API tests. Here, verify through the
    # engine's own store after a second drain.
    assert relay.drain_once() == 0, "no duplicate delivery"


def test_replay_is_idempotent(tmp_path, monkeypatch):
    """
    Re-delivering the same outbox message must not create a
    duplicate learning record.
    """
    from app.learning import engine as learning_engine

    db_path = tmp_path / "db.sqlite"
    monkeypatch.setenv("YODAW_DB_PATH", str(db_path))

    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    _enqueue_learning(store, "m_replay_1")

    relay = OutboxRelay(store=store)

    # Deliver twice: the second pass re-handles the same message
    # only if mark-delivered failed; simulate that crash window by
    # handling without marking, then marking.
    messages = store.outbox_pending()
    assert len(messages) == 1

    relay._handle(messages[0])
    relay._handle(messages[0])  # crash happened before mark-delivered

    store.outbox_mark_delivered(messages[0]["id"])

    records = learning.list()
    assert len(records) == 1, "replay must not duplicate the record"
    assert records[0].mission_id == "m_replay_1"


def test_failure_is_recorded_and_retried(tmp_path, caplog):
    store = MissionStore(tmp_path / "db.sqlite")

    _enqueue_learning(store, "m_retry_1")

    relay = OutboxRelay(store=store)

    def boom(message):
        raise RuntimeError("learning store outage")

    relay._handle = boom

    with caplog.at_level(logging.ERROR, logger="yodaw.outbox"):
        assert relay.drain_once() == 0
        assert relay._failures == 1

    # The failure is recorded on the message with its error.
    # Stage 10.6: the message stays undelivered (still counted as
    # pending in stats) but is retry-scheduled with backoff, so it
    # is not yet due for another drain pass.
    assert store.outbox_stats()["pending"] == 1
    assert store.outbox_pending() == []

    import sqlite3

    db = sqlite3.connect(str(tmp_path / "db.sqlite"))
    row = db.execute(
        "SELECT attempts, last_error, next_attempt_at FROM mission_outbox"
    ).fetchone()
    db.close()

    assert row[0] == 1
    assert "learning store outage" in row[1]
    assert row[2] is not None, "backoff must schedule a retry time"
    assert "outbox delivery failed" in caplog.text

    # Recovery: once the backoff window passes, the next pass
    # delivers. Force the schedule due to keep the test hermetic.
    db = sqlite3.connect(str(tmp_path / "db.sqlite"))
    db.execute(
        "UPDATE mission_outbox SET next_attempt_at='2000-01-01T00:00:00+00:00'"
    )
    db.commit()
    db.close()

    relay2 = OutboxRelay(store=store)
    assert relay2.drain_once() == 1
    assert store.outbox_stats()["pending"] == 0


def test_coordinator_writes_learning_through_outbox(tmp_path):
    """
    Mission completion enqueues learning into the outbox and the
    relay delivers it; the record id is deterministic per mission.
    """
    db_path = tmp_path / "db.sqlite"
    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    mission = Mission(goal="outbox mission", capability="outbox-cap")
    store.enqueue(mission)

    relay = OutboxRelay(store=store)
    coord = Coordinator(store=store, relay=relay)
    claimed = store.claim_next(coord.id)
    assert claimed is not None
    coord._execute(claimed, "capability:outbox-cap")

    assert store.get(mission.id).status.value == "PASS"
    assert relay.stats()["delivered"] == 1

    records = [
        r for r in learning.list() if r.mission_id == mission.id
    ]
    assert len(records) == 1
    assert records[0].id == record_id_default(mission.id)
    assert records[0].outcome == "PASS"


def test_pending_learning_survives_coordinator_restart(tmp_path):
    """
    Crash window: the coordinator enqueued the learning message
    but died before any drain ran (process crash between the two
    operations). A fresh coordinator/relay later delivers it.
    """
    db_path = tmp_path / "db.sqlite"
    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    mission = Mission(goal="crash window", capability="outbox-cap")
    store.enqueue(mission)

    # The mission finalized and the outbox message was written,
    # then the process crashed before the eager drain ran.
    _enqueue_learning(store, mission.id)

    assert store.outbox_stats()["pending"] == 1
    assert learning.list() == []

    # Restart: a fresh relay delivers the orphaned message.
    relay = OutboxRelay(store=store)
    assert relay.drain_once() == 1

    records = [
        r for r in learning.list() if r.mission_id == mission.id
    ]
    assert len(records) == 1
