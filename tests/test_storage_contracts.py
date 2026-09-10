"""
Stage 10.1: adapter contract tests.

The capability protocols (app/storage/protocols.py) define one
contract per storage area. These tests verify the SQLite adapter
implements the full contract; the same suite runs against
Postgres in test_pg_contract.py when a server is reachable.
"""

from __future__ import annotations

import threading

import pytest

from app.core.models import Mission, MissionStatus
from app.runtime.repo_leases import RepoLeaseManager
from app.storage.protocols import (
    AuditStoreProtocol,
    ClientStoreProtocol,
    LeaseManagerProtocol,
    LearningStoreProtocol,
    MissionStoreProtocol,
)
from app.storage.sqlite_store import MissionStore, DuplicateMission
from app.tenants.audit import AuditStore
from app.tenants.clients import ClientStore


def test_mission_store_satisfies_protocol(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    assert isinstance(store, MissionStoreProtocol)


def test_lease_manager_satisfies_protocol(tmp_path):
    leases = RepoLeaseManager(tmp_path / "db.sqlite")

    assert isinstance(leases, LeaseManagerProtocol)


def test_client_store_satisfies_protocol(tmp_path):
    store = ClientStore(tmp_path / "clients.sqlite")

    assert isinstance(store, ClientStoreProtocol)


def test_audit_store_satisfies_protocol(tmp_path):
    store = AuditStore(tmp_path / "audit.sqlite")

    assert isinstance(store, AuditStoreProtocol)


def test_learning_store_satisfies_protocol(tmp_path, monkeypatch):
    from app.learning.store import LearningStore

    store = LearningStore(tmp_path / "db.sqlite")

    assert isinstance(store, LearningStoreProtocol)


def test_protocol_contract_enqueue_claim_cancel(tmp_path):
    """Protocol-level queue contract, backend-agnostic shape."""
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(
        goal="contract mission",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)

    claimed = store.claim_next("coord-1")

    assert claimed is not None
    assert claimed.id == mission.id
    assert claimed.status == MissionStatus.running
    assert store.claim_next("coord-2") is None

    assert store.request_cancel(claimed.id) == "requested"
    assert store.heartbeat(claimed.id, "coord-1") is True
    assert store.heartbeat(claimed.id, "coord-2") is False


def test_protocol_contract_outbox_operations(tmp_path):
    """Outbox contract: enqueue/idempotency/pending/requeue/stats."""
    store = MissionStore(tmp_path / "db.sqlite")

    first = store.outbox_enqueue(
        mission_id="m1",
        kind="learning.record",
        payload={"goal": "g", "success": True},
        idempotency_key="idem-1",
    )
    # Idempotent replay returns the original message id.
    replay = store.outbox_enqueue(
        mission_id="m1",
        kind="learning.record",
        payload={"goal": "g", "success": True},
        idempotency_key="idem-1",
    )
    assert first == replay

    assert len(store.outbox_pending()) == 1

    store.outbox_dead_letter(first)
    assert store.outbox_pending() == []
    assert store.outbox_stats()["dead_lettered"] == 1

    assert store.outbox_requeue_dead(first) == 1
    assert len(store.outbox_pending()) == 1

    message = store.outbox_message(first)
    assert message is not None
    assert message["kind"] == "learning.record"
    assert message["dead_lettered_at"] is None

    store.outbox_mark_delivered(first)
    assert store.outbox_pending() == []
    assert store.outbox_stats()["delivered"] == 1


def test_protocol_contract_outbox_kind_filter(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    store.outbox_enqueue(
        mission_id="m1", kind="learning.record", payload={}
    )
    store.outbox_enqueue(
        mission_id="m1", kind="webhook.delivery", payload={}
    )

    learning_only = store.outbox_pending(kinds=["learning.record"])

    assert len(learning_only) == 1
    assert learning_only[0]["kind"] == "learning.record"

    webhooks = store.outbox_pending(kinds=["webhook.delivery"])

    assert len(webhooks) == 1
    assert webhooks[0]["kind"] == "webhook.delivery"


def test_protocol_contract_rate_limit_bucket(tmp_path):
    """Token bucket contract: burst, refill, per-bucket isolation."""
    store = MissionStore(tmp_path / "db.sqlite")

    now = 1000.0

    # capacity 2: two takes pass, third must be refused with a
    # retry window.
    ok1, _ = store.rate_limit_take(
        "client-a", now=now, capacity=2, refill_per_second=1.0
    )
    ok2, _ = store.rate_limit_take(
        "client-a", now=now, capacity=2, refill_per_second=1.0
    )
    ok3, retry_after = store.rate_limit_take(
        "client-a", now=now, capacity=2, refill_per_second=1.0
    )

    assert ok1 and ok2
    assert not ok3
    assert retry_after > 0

    # Independent bucket for another client.
    ok_other, _ = store.rate_limit_take(
        "client-b", now=now, capacity=2, refill_per_second=1.0
    )
    assert ok_other

    # Time passes, tokens refill (2 seconds -> 2 tokens back).
    ok_refill, _ = store.rate_limit_take(
        "client-a", now=now + 2.0, capacity=2, refill_per_second=1.0
    )
    assert ok_refill


def test_protocol_contract_rate_limit_shared_across_stores(tmp_path):
    """Two store instances (simulating two processes) share one
    bucket through the database."""
    db = tmp_path / "db.sqlite"
    store_a = MissionStore(db)
    store_b = MissionStore(db)

    now = 500.0

    ok1, _ = store_a.rate_limit_take(
        "shared", now=now, capacity=1, refill_per_second=0.1
    )
    ok2, _ = store_b.rate_limit_take(
        "shared", now=now, capacity=1, refill_per_second=0.1
    )

    assert ok1
    assert not ok2, "second process must see the same bucket"


def test_protocol_contract_audit_chain_and_pagination(tmp_path):
    audit = AuditStore(tmp_path / "audit.sqlite")

    for i in range(5):
        audit.append(
            client_id="cl_1",
            action="mission.created",
            mission_id=f"m_{i}",
            data={"i": i},
        )

    page1 = audit.query(limit=2, offset=0)
    page2 = audit.query(limit=2, offset=2)

    assert [e["seq"] for e in page1] == [1, 2]
    assert [e["seq"] for e in page2] == [3, 4]

    assert audit.verify()["intact"] is True


def test_protocol_contract_audit_detects_tampering(tmp_path):
    """Deleting or mutating a row breaks the chain at that seq."""
    import sqlite3

    db_path = tmp_path / "audit.sqlite"
    audit = AuditStore(db_path)

    audit.append(client_id="cl_1", action="a1", data={})
    audit.append(client_id="cl_1", action="a2", data={})
    audit.append(client_id="cl_1", action="a3", data={})

    # Mutate the middle event in storage (simulating tampering).
    db = sqlite3.connect(str(db_path))
    db.execute(
        "UPDATE audit_events SET action='FORGERY' WHERE seq=2"
    )
    db.commit()
    db.close()

    result = audit.verify()

    assert result["intact"] is False
    assert result["broken_at"] == 2


def test_protocol_contract_audit_prune_preserves_survivors(tmp_path):
    """Prune removes only old rows and the survivor chain stays
    verifiable from its first surviving event."""
    from datetime import datetime, timedelta, timezone
    import sqlite3

    db_path = tmp_path / "audit.sqlite"
    audit = AuditStore(db_path)

    audit.append(client_id="cl_1", action="old-event", data={})

    # Backdate the first event beyond any keep window.
    db = sqlite3.connect(str(db_path))
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
    db.execute("UPDATE audit_events SET ts=? WHERE seq=1", (old_ts,))
    db.commit()
    db.close()

    audit.append(client_id="cl_1", action="new-event", data={})

    result = audit.prune(keep_days=7)

    assert result["pruned"] == 1
    assert audit.count() == 1
    assert audit.query()[0]["action"] == "new-event"
    assert audit.verify()["intact"] is True


def test_protocol_contract_audit_prune_archives_boundary(tmp_path):
    import json as _json
    from datetime import datetime, timedelta, timezone
    import sqlite3

    db_path = tmp_path / "audit.sqlite"
    audit = AuditStore(db_path)
    archive = tmp_path / "archive.jsonl"

    audit.append(client_id="cl_1", action="old-event", data={"k": 1})

    db = sqlite3.connect(str(db_path))
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat()
    db.execute("UPDATE audit_events SET ts=? WHERE seq=1", (old_ts,))
    db.commit()
    db.close()

    result = audit.prune(keep_days=7, archive_path=archive)

    assert result["archived"] == 1
    assert result["head_seq"] == 1
    assert result["head_hash"]

    lines = [
        _json.loads(line)
        for line in archive.read_text().splitlines()
        if line.strip()
    ]

    # First line is the verifiable boundary metadata; second is
    # the event itself with its chain hashes.
    boundary = lines[0]["boundary"]

    assert boundary["pruned_through_seq"] == 1
    assert boundary["pruned_through_hash"] == result["head_hash"]

    archived_event = lines[1]

    assert archived_event["action"] == "old-event"
    assert archived_event["event_hash"] == result["head_hash"]


def test_protocol_contract_duplicate_mission_single_flight(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(
        goal="same goal",
        capability="repo-code",
        metadata={"repo_path": "/repo/x"},
    )
    store.enqueue(mission)

    with pytest.raises(DuplicateMission):
        store.enqueue(
            Mission(
                goal="same goal",
                capability="repo-code",
                metadata={"repo_path": "/repo/x"},
            )
        )


def test_protocol_contract_stale_executing_recovery_view(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(
        goal="stale mission",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)
    store.claim_next("coord-1")

    # Fresh heartbeat: not stale.
    assert store.stale_executing(600) == []

    # Absurdly tight cutoff: stale.
    stale = store.stale_executing(0)

    assert len(stale) == 1
    assert stale[0].id == mission.id
