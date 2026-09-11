"""Final reliability hardening regressions (P1-1/P1-2/P1-3).

Hermetic: SQLite only, no network, no provider.
"""

import sqlite3
import threading
import uuid

from app.core.models import Mission, MissionStatus
from app.runtime.coordinator import Coordinator
from app.runtime.outbox_relay import OutboxRelay
from app.storage.sqlite_store import (
    InvalidStateError,
    MissionStore,
    StaleOwnerError,
)


def _mission(goal=None, repo=None, **kw):
    return Mission(
        goal=goal or f"g-{uuid.uuid4().hex}",
        capability="code",
        metadata={"repo_path": repo or f"/repo/{uuid.uuid4().hex}"},
        **kw,
    )


# ------------------------------------------------- P1-1

def test_p1_1_first_request_creates_one_mission(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    mission = _mission()
    stored, replayed = store.submit_idempotent_mission(
        mission, tenant_scope="t1", idempotency_key="k1"
    )
    assert replayed is False
    assert stored.id == mission.id
    assert store.get(mission.id) is not None
    assert store.idempotency_lookup("t1", "k1") == mission.id


def test_p1_1_replay_returns_same_mission(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    mission = _mission()
    store.submit_idempotent_mission(
        mission, tenant_scope="t1", idempotency_key="k1"
    )
    other = _mission(repo="/repo/other")
    stored, replayed = store.submit_idempotent_mission(
        other, tenant_scope="t1", idempotency_key="k1"
    )
    assert replayed is True
    assert stored.id == mission.id
    assert store.get(other.id) is None


def test_p1_1_concurrent_same_key_single_mission(tmp_path):
    db = tmp_path / "db.sqlite"
    MissionStore(db)
    results, errors = [], []

    def submit(i):
        try:
            store = MissionStore(db)
            m = Mission(
                goal=f"goal-{i}-{uuid.uuid4().hex}",
                capability="code",
                metadata={"repo_path": f"/repo/race/{i}"},
            )
            stored, _ = store.submit_idempotent_mission(
                m, tenant_scope="t1", idempotency_key="race-key"
            )
            results.append(stored.id)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == 8
    assert len(set(results)) == 1


def test_p1_1_cross_tenant_same_key_isolated(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    a = _mission()
    b = _mission()
    sa, ra = store.submit_idempotent_mission(
        a, tenant_scope="tenant-a", idempotency_key="shared"
    )
    sb, rb = store.submit_idempotent_mission(
        b, tenant_scope="tenant-b", idempotency_key="shared"
    )
    assert (sa.id, ra) == (a.id, False)
    assert (sb.id, rb) == (b.id, False)
    assert sa.id != sb.id


def test_p1_1_exception_before_commit_leaves_neither(tmp_path):
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    mission = _mission()
    with sqlite3.connect(str(db)) as raw:
        raw.execute("BEGIN IMMEDIATE")
        raw.execute(
            "INSERT INTO missions(id, payload, status, goal) "
            "VALUES(?, ?, 'QUEUED', ?)",
            (mission.id, mission.model_dump_json(), mission.goal),
        )
        raw.rollback()
    assert store.get(mission.id) is None
    assert store.idempotency_lookup("t1", "k-never") is None


def test_p1_1_no_dangling_mapping_after_failed_submit(tmp_path):
    from app.storage.sqlite_store import DuplicateMission

    store = MissionStore(tmp_path / "db.sqlite")
    first = Mission(
        goal="same", capability="code",
        metadata={"repo_path": "/repo/dup"},
    )
    store.enqueue(first)
    second = Mission(
        goal="same", capability="code",
        metadata={"repo_path": "/repo/dup"},
    )
    try:
        store.submit_idempotent_mission(
            second, tenant_scope="t1", idempotency_key="k-dup"
        )
        raise AssertionError("expected DuplicateMission")
    except DuplicateMission:
        pass
    assert store.idempotency_lookup("t1", "k-dup") is None
    assert store.get(second.id) is None


def test_p1_1_dry_run_idempotent_replay_safe(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    mission = _mission()
    mission.status = MissionStatus.passed
    mission.result = {"dry_run": True}
    stored, replayed = store.submit_idempotent_mission(
        mission, tenant_scope="t1", idempotency_key="k-dry"
    )
    assert replayed is False
    other = _mission(repo="/repo/dry2")
    stored2, replayed2 = store.submit_idempotent_mission(
        other, tenant_scope="t1", idempotency_key="k-dry"
    )
    assert replayed2 is True
    assert stored2.id == stored.id


# ------------------------------------------------- P1-2

def _claimed(tmp_path, owner="coord-1"):
    db = tmp_path / f"{uuid.uuid4().hex}.sqlite"
    store = MissionStore(db)
    mission = _mission()
    store.enqueue(mission)
    claimed = store.claim_next(owner)
    assert claimed is not None
    return store, claimed


def test_p1_2_success_commits_all_three(tmp_path):
    store, claimed = _claimed(tmp_path)
    finalized = store.finalize_mission(
        claimed.id,
        owner=claimed.claimed_by,
        status=MissionStatus.passed,
        result={"ok": True},
        evidence=[{"t": "w"}],
        outbox_payload={"goal": claimed.goal, "success": True},
    )
    assert finalized.status == MissionStatus.passed
    assert store.get(claimed.id).status == MissionStatus.passed
    types = [e["event_type"] for e in store.events(claimed.id)]
    assert "mission.completed" in types
    pending = store.outbox_pending()
    assert len(pending) == 1
    assert pending[0]["mission_id"] == claimed.id


def test_p1_2_failure_before_commit_no_terminal(tmp_path):
    store, claimed = _claimed(tmp_path)
    before = store.get(claimed.id)
    try:
        raise RuntimeError("boom before commit")
    except RuntimeError:
        pass
    after = store.get(claimed.id)
    assert after.status == before.status
    assert after.status not in (
        MissionStatus.passed, MissionStatus.failed,
        MissionStatus.blocked, MissionStatus.blocked_external,
        MissionStatus.cancelled,
    )
    assert "mission.completed" not in [
        e["event_type"] for e in store.events(claimed.id)
    ]
    assert store.outbox_pending() == []


def test_p1_2_failure_during_event_no_partial(tmp_path, monkeypatch):
    store, claimed = _claimed(tmp_path)
    import sqlite3 as _sqlite

    real_connect = _sqlite.connect

    def boom(*a, **k):
        raise RuntimeError("event insert down")

    monkeypatch.setattr(
        "app.storage.db.connect",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("event insert down")
        ),
    )
    try:
        store.finalize_mission(
            claimed.id,
            owner=claimed.claimed_by,
            status=MissionStatus.passed,
            result={},
            evidence=[],
            outbox_payload={"goal": "g", "success": True},
        )
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    fresh = MissionStore(store.path)
    assert fresh.get(claimed.id).status == claimed.status
    assert "mission.completed" not in [
        e["event_type"] for e in fresh.events(claimed.id)
    ]


def test_p1_2_failure_during_outbox_no_partial(tmp_path, monkeypatch):
    import app.storage.sqlite_store as mod

    store, claimed = _claimed(tmp_path)
    real_finalize = mod.MissionStore.finalize_mission

    def boom(self, *a, **k):
        raise RuntimeError("outbox insert down")

    monkeypatch.setattr(mod.MissionStore, "finalize_mission", boom)
    try:
        store.finalize_mission(
            claimed.id,
            owner=claimed.claimed_by,
            status=MissionStatus.passed,
            result={},
            evidence=[],
            outbox_payload={"goal": "g", "success": True},
        )
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    monkeypatch.setattr(
        mod.MissionStore, "finalize_mission", real_finalize
    )
    fresh = MissionStore(store.path)
    assert fresh.get(claimed.id).status == claimed.status
    assert fresh.outbox_pending() == []


def test_p1_2_repeated_finalize_idempotent_or_rejected(tmp_path):
    store, claimed = _claimed(tmp_path)
    store.finalize_mission(
        claimed.id,
        owner=claimed.claimed_by,
        status=MissionStatus.passed,
        result={"ok": 1},
        evidence=[],
        outbox_payload={"goal": claimed.goal, "success": True},
    )
    # Same terminal replay: idempotent, no dup outbox.
    again = store.finalize_mission(
        claimed.id,
        owner=claimed.claimed_by,
        status=MissionStatus.passed,
        result={"ok": 1},
        evidence=[],
        outbox_payload={"goal": claimed.goal, "success": True},
    )
    assert again.status == MissionStatus.passed
    assert len(store.outbox_pending()) == 1
    # Different terminal over terminal: safely rejected.
    try:
        store.finalize_mission(
            claimed.id,
            owner=claimed.claimed_by,
            status=MissionStatus.failed,
            result={},
            evidence=[],
            outbox_payload={"goal": "g", "success": False},
        )
        raise AssertionError("expected InvalidStateError")
    except InvalidStateError:
        pass


def test_p1_2_exactly_one_learning_message(tmp_path):
    store, claimed = _claimed(tmp_path)
    for _ in range(3):
        store.finalize_mission(
            claimed.id,
            owner=claimed.claimed_by,
            status=MissionStatus.passed,
            result={"ok": 1},
            evidence=[],
            outbox_payload={"goal": claimed.goal, "success": True},
        )
    rows = [
        m for m in store.outbox_pending()
        if m["mission_id"] == claimed.id
    ]
    assert len(rows) == 1


# ------------------------------------------------- P1-3

def test_p1_3_stale_owner_write_rejected(tmp_path):
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    mission = _mission()
    store.enqueue(mission)
    claimed_a = store.claim_next("owner-A")
    assert claimed_a is not None
    # Ownership recovered to B (watchdog-style steal).
    raw = MissionStore(db)
    victim = raw.get(mission.id)
    victim.claimed_by = "owner-B"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE missions SET claimed_by='owner-B', "
            "payload=json_set(payload,'$.claimed_by','owner-B') "
            "WHERE id=?",
            (mission.id,),
        )
        conn.commit()
    stale = store.get(mission.id)
    stale.claimed_by = "owner-A"
    stale.result = {"hijacked": True}
    try:
        store.save_owned(stale, "owner-A")
        raise AssertionError("expected StaleOwnerError")
    except StaleOwnerError:
        pass
    try:
        store.finalize_mission(
            mission.id,
            owner="owner-A",
            status=MissionStatus.passed,
            result={"hijacked": True},
            evidence=[],
            outbox_payload={"goal": "g", "success": True},
        )
        raise AssertionError("expected StaleOwnerError")
    except StaleOwnerError:
        pass
    current = store.get(mission.id)
    assert current.result != {"hijacked": True}
    assert current.claimed_by == "owner-B"
    assert current.status == claimed_a.status


def test_p1_3_evidence_preserved_and_appended(tmp_path):
    store, claimed = _claimed(tmp_path)
    prior = [{"type": "recovery_inspection", "n": 1}]
    cur = store.get(claimed.id)
    cur.evidence = prior
    store.save_owned(cur, cur.claimed_by)
    merged = Coordinator._merged_evidence(prior, [{"type": "worker", "n": 2}])
    finalized = store.finalize_mission(
        claimed.id,
        owner=claimed.claimed_by,
        status=MissionStatus.passed,
        result={"ok": 1},
        evidence=merged,
        outbox_payload={"goal": claimed.goal, "success": True},
    )
    types = [e.get("type") for e in finalized.evidence]
    assert "recovery_inspection" in types
    assert "worker" in types
    assert types.index("recovery_inspection") < types.index("worker")


def test_p1_3_retry_evidence_survives_and_no_dup(tmp_path):
    merged = Coordinator._merged_evidence(
        [{"type": "a"}, {"type": "b"}],
        [{"type": "b"}, {"type": "c"}],
    )
    assert [e["type"] for e in merged] == ["a", "b", "c"]


def test_p1_3_terminal_contains_complete_history(tmp_path):
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    relay = OutboxRelay(store=store)
    learning_mod = __import__(
        "app.learning.store", fromlist=["LearningStore"]
    )
    learning = learning_mod.LearningStore(db)
    mission = Mission(goal="history", capability="hist-cap")
    store.enqueue(mission)

    class HistWorker:
        name = "hist-bud"
        capabilities = {"hist-cap"}

        def supports(self, capability):
            return capability in self.capabilities

        def health(self):
            return {"status": "READY"}

        def execute(self, goal, metadata=None):
            return {
                "success": True,
                "output": {"goal": goal},
                "evidence": [{"type": "worker-step"}],
                "error": None,
            }

    import app.workers.registry as reg

    reg.registry.workers.append(HistWorker())
    try:
        coord = Coordinator(store=store, relay=relay)
        claimed = store.claim_next(coord.id)
        assert claimed is not None
        # Simulate pre-existing recovery evidence.
        cur = store.get(claimed.id)
        cur.evidence = [{"type": "recovery_inspection"}]
        store.save_owned(cur, cur.claimed_by)
        coord._execute(store.get(claimed.id), "capability:hist-cap")
    finally:
        reg.registry.workers.pop()
    final = store.get(mission.id)
    assert final.status == MissionStatus.passed
    types = [e.get("type") for e in final.evidence]
    assert "recovery_inspection" in types
    assert "worker-step" in types
    assert "product_context" in types
