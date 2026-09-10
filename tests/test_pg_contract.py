"""
Stage 10.2: Postgres adapter contract.

Two layers:

1. Hermetic tests (always run): the Postgres adapter module must
   import, expose the same capability surface as SQLite, and
   compile its SQL against the same protocol contract without a
   server.

2. Real-server tests (pytest.mark.skipif when unreachable): run
   the shared contract suite against an actual PostgreSQL. Set
   YODAW_TEST_PG_DSN (e.g. postgresql://yodaw:yodaw@localhost:5432/
   yodaw_test) to enable; otherwise the integration evidence is
   reported as BLOCKED_EXTERNAL and never silently upgraded.
"""

from __future__ import annotations

import os

import pytest

from app.storage.protocols import MissionStoreProtocol

psycopg = pytest.importorskip(
    "psycopg", reason="psycopg not installed"
)

from app.storage.pg_store import (  # noqa: E402
    PostgresAuditStore,
    PostgresMissionStore,
    PostgresStores,
)


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2) as db:
            db.execute("SELECT 1")
        return True
    except Exception:
        return False


TEST_DSN = os.environ.get(
    "YODAW_TEST_PG_DSN",
    "postgresql://yodaw:yodaw@localhost:5432/yodaw_test",
)
PG_AVAILABLE = _pg_reachable(TEST_DSN)


# ---------------------------------------------------------
# Hermetic layer (no server required)
# ---------------------------------------------------------


def test_pg_store_satisfies_mission_protocol_hermetically():
    """The adapter class implements the mission/queue/outbox
    protocol without a server: constructor signature, method
    surface, and shared policy constants."""
    from app.storage.sqlite_store import (
        OUTBOX_BACKOFF_BASE_SECONDS,
        OUTBOX_BACKOFF_MAX_SECONDS,
        OUTBOX_MAX_ATTEMPTS,
    )

    methods = {
        name
        for name in MissionStoreProtocol.__protocol_attrs__  # type: ignore[attr-defined]
        if not name.startswith("_")
    }

    for name in methods:
        assert hasattr(PostgresMissionStore, name), (
            f"Postgres adapter missing protocol method: {name}"
        )

    # Shared relay policy: identical backoff behavior across
    # backends (single source of truth in the sqlite module).
    from app.storage import pg_store

    assert pg_store.OUTBOX_MAX_ATTEMPTS is OUTBOX_MAX_ATTEMPTS
    assert (
        pg_store.OUTBOX_BACKOFF_BASE_SECONDS
        is OUTBOX_BACKOFF_BASE_SECONDS
    )
    assert (
        pg_store.OUTBOX_BACKOFF_MAX_SECONDS
        is OUTBOX_BACKOFF_MAX_SECONDS
    )


def test_pg_store_claim_uses_skip_locked_semantics():
    """The exact-once claim SQL must carry FOR UPDATE SKIP LOCKED
    (inspectable without a server)."""
    import inspect

    source = inspect.getsource(PostgresMissionStore.claim_next)

    assert "FOR UPDATE SKIP LOCKED" in source
    assert "ORDER BY priority ASC, created_at ASC" in source


def test_pg_store_outbox_insert_is_idempotent_by_key():
    import inspect

    source = inspect.getsource(PostgresMissionStore.outbox_enqueue)

    assert "ON CONFLICT" in source
    assert "idempotency_key" in source


def test_pg_rate_limit_take_locks_bucket_row():
    import inspect

    source = inspect.getsource(PostgresMissionStore.rate_limit_take)

    assert "FOR UPDATE" in source


def test_pg_missing_psycopg_raises_clear_error(monkeypatch):
    """Without the driver, constructing the store fails with an
    actionable message instead of a stack trace deep in psycopg."""
    from app.storage import pg_store

    monkeypatch.setattr(pg_store, "psycopg", None)

    with pytest.raises(RuntimeError, match="psycopg is required"):
        PostgresMissionStore(
            "postgresql://yodaw:yodaw@localhost:5432/yodaw_test"
        )


# ---------------------------------------------------------
# Real-server layer (BLOCKED_EXTERNAL when unavailable)
# ---------------------------------------------------------


@pytest.fixture()
def pg_stores():
    if not PG_AVAILABLE:
        pytest.skip(
            "POSTGRES_INTEGRATION = BLOCKED_EXTERNAL: no PostgreSQL "
            "reachable at YODAW_TEST_PG_DSN; adapter contract is "
            "covered hermetically"
        )

    from app.storage.pg_store import PostgresStores

    stores = PostgresStores(TEST_DSN)

    # Clean slate per test (schema survives; rows go).
    with psycopg.connect(TEST_DSN, autocommit=True) as db:
        db.execute("TRUNCATE missions, mission_events, mission_outbox")
        db.execute("TRUNCATE audit_events")
        db.execute("TRUNCATE api_clients, api_admins")
        db.execute("TRUNCATE rate_limit_buckets")

    yield stores


def test_pg_mission_queue_claim_contract(pg_stores):
    store = pg_stores.store

    from app.core.models import Mission, MissionStatus

    mission = Mission(
        goal="pg contract mission",
        capability="repo-code",
        metadata={"repo_path": "/repo/pg"},
    )
    store.enqueue(mission)

    claimed = store.claim_next("pg-coord-1")

    assert claimed is not None
    assert claimed.id == mission.id
    assert claimed.status == MissionStatus.running
    assert store.claim_next("pg-coord-2") is None

    assert store.heartbeat(claimed.id, "pg-coord-1") is True
    assert store.heartbeat(claimed.id, "other") is False

    assert store.request_cancel(claimed.id) == "requested"
    assert store.get(mission.id).cancel_requested is True


def test_pg_concurrent_claims_exact_once(pg_stores):
    store = pg_stores.store

    import threading

    from app.core.models import Mission

    total = 12

    for i in range(total):
        store.enqueue(
            Mission(
                goal=f"pg goal {i}",
                capability="repo-code",
                metadata={"repo_path": f"/repo/pg/{i}"},
            )
        )

    claimed = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def claimer(name):
        barrier.wait()

        while True:
            mission = store.claim_next(name)

            if mission is None:
                return

            with lock:
                claimed.append(mission.id)

    threads = [
        threading.Thread(target=claimer, args=(f"pg-c{i}",))
        for i in range(4)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    assert len(claimed) == total
    assert len(set(claimed)) == total, "mission claimed twice"


def test_pg_quota_enforced_at_claim(pg_stores):
    store = pg_stores.store

    import threading

    from app.core.models import Mission

    for i in range(4):
        store.enqueue(
            Mission(
                goal=f"pg quota {i}",
                capability="repo-code",
                metadata={"repo_path": f"/repo/q/{i}"},
                client_id="cl_pg",
                priority=1,
            )
        )

    claimed = []

    def claimer(name):
        while True:
            mission = store.claim_next(
                name, client_limits={"cl_pg": 2}
            )

            if mission is None:
                return

            claimed.append(mission.id)

    threads = [
        threading.Thread(target=claimer, args=(f"pg-q{i}",))
        for i in range(3)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    assert len(claimed) == 2, "quota must bound claims exactly"


def test_pg_outbox_contract(pg_stores):
    store = pg_stores.store

    first = store.outbox_enqueue(
        mission_id="m_pg",
        kind="learning.record",
        payload={"goal": "g", "success": True},
        idempotency_key="pg-idem-1",
    )
    replay = store.outbox_enqueue(
        mission_id="m_pg",
        kind="learning.record",
        payload={"goal": "g", "success": True},
        idempotency_key="pg-idem-1",
    )

    assert first == replay

    assert len(store.outbox_pending()) == 1

    # Failure schedules backoff; forcing it due retries.
    store.outbox_mark_failed(first, "RuntimeError: pg outage")

    assert store.outbox_pending() == []

    fresh = store.outbox_message(first)

    assert fresh["attempts"] == 1
    assert "pg outage" in fresh["last_error"]
    assert fresh["next_attempt_at"] is not None

    store.outbox_dead_letter(first)
    assert store.outbox_stats()["dead_lettered"] == 1

    assert store.outbox_requeue_dead() == 1
    assert len(store.outbox_pending()) == 1

    store.outbox_mark_delivered(first)
    assert store.outbox_stats()["delivered"] == 1


def test_pg_audit_chain_contract(pg_stores):
    audit = pg_stores.audit

    for i in range(4):
        audit.append(
            client_id="cl_pg",
            actor="ops",
            action="mission.created",
            mission_id=f"m_{i}",
            data={"i": i},
        )

    result = audit.verify()

    assert result["intact"] is True
    assert result["verified_through"] == 4

    page = audit.query(limit=2, offset=2)

    assert [e["seq"] for e in page] == [3, 4]
    assert page[0]["actor"] == "ops"


def test_pg_rate_limit_contract(pg_stores):
    store = pg_stores.store

    now = 900.0

    ok1, _ = store.rate_limit_take(
        "pg-client", now=now, capacity=2, refill_per_second=1.0
    )
    ok2, _ = store.rate_limit_take(
        "pg-client", now=now, capacity=2, refill_per_second=1.0
    )
    ok3, retry_after = store.rate_limit_take(
        "pg-client", now=now, capacity=2, refill_per_second=1.0
    )

    assert ok1 and ok2
    assert not ok3
    assert retry_after > 0

    # Shared across separate adapter instances (processes).
    from app.storage.pg_store import PostgresMissionStore

    second = PostgresMissionStore(TEST_DSN)
    ok_other, _ = second.rate_limit_take(
        "pg-client", now=now, capacity=2, refill_per_second=1.0
    )

    assert not ok_other, "bucket must be shared, not per-adapter"


def test_pg_migrations_are_versioned(pg_stores):
    with psycopg.connect(TEST_DSN) as db:
        row = db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()

    assert row[0] >= 1
