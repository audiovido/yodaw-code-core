"""
Stage 9.1-9.3: client identities, quotas, and audit trail.

Hermetic tests for the multi-tenant foundation: hashed-key
identity resolution, revoke/re-enable, priority classes, quota
admission, append-only audit with secret redaction, and the admin
authorization boundary (client keys must never manage clients).
"""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.tenants.audit import AuditStore
from app.tenants.clients import ClientStore, hash_key
from app.tenants.redact import REDACTED, redact


def test_client_lifecycle_hashed_keys_and_revocation(tmp_path):
    store = ClientStore(tmp_path / "clients.db")

    created = store.create_client("acme", priority=2)

    assert created["api_key"].startswith("yodak_")
    assert created["priority"] == 2

    record = store.authenticate(created["api_key"])

    assert record is not None
    assert record.name == "acme"

    # Only the SHA-256 hash is persisted; the raw key must not be
    # recoverable from storage.
    import sqlite3

    db = sqlite3.connect(str(tmp_path / "clients.db"))
    rows = db.execute(
        "SELECT key_hash FROM api_clients WHERE name='acme'"
    ).fetchall()
    db.close()

    assert rows[0][0] == hash_key(created["api_key"])
    assert created["api_key"].encode() not in (
        rows[0][0].encode()
    )

    # Revocation: disabled clients fail authentication and are
    # indistinguishable from unknown keys.
    assert store.set_disabled("acme", True) is True
    assert store.authenticate(created["api_key"]) is None

    # Re-enable restores access.
    assert store.set_disabled("acme", False) is True
    assert store.authenticate(created["api_key"]) is not None

    # Unknown keys never authenticate.
    assert store.authenticate("yodak_unknown") is None
    assert store.authenticate("") is None


def test_duplicate_client_name_rejected(tmp_path):
    store = ClientStore(tmp_path / "clients.db")

    store.create_client("acme")

    with pytest.raises(ValueError):
        store.create_client("acme")


def test_priority_clamped_to_valid_range(tmp_path):
    store = ClientStore(create := tmp_path / "clients.db")

    created = store.create_client("edge", priority=99)

    assert created["priority"] == 9

    store.set_priority("edge", 0)

    listing = [c for c in store.list_clients() if c["name"] == "edge"]

    assert listing[0]["priority"] == 1


def test_audit_append_only_and_redacted(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")

    audit.append(
        client_id="cl_1",
        action="mission.created",
        mission_id="m_1",
        data={
            "api_key": "yodak_SUPERSECRET",
            "Authorization": "Bearer topsecret",
            "nested": {"password": "hunter2", "goal": "fine"},
            "items": [{"token": "leak-me"}, 7],
        },
    )

    rows = audit.query(client_id="cl_1")

    assert len(rows) == 1

    row = rows[0]

    assert row["action"] == "mission.created"
    assert row["mission_id"] == "m_1"
    assert row["data"]["api_key"] == REDACTED
    assert row["data"]["Authorization"] == REDACTED
    assert row["data"]["nested"]["password"] == REDACTED
    assert row["data"]["nested"]["goal"] == "fine"
    assert row["data"]["items"][0]["token"] == REDACTED
    assert row["data"]["items"][1] == 7

    # The plaintext secrets must not appear anywhere in storage.
    import sqlite3

    db = sqlite3.connect(str(tmp_path / "audit.db"))
    blobs = "\n".join(
        r[0] for r in db.execute("SELECT data FROM audit_events")
    )
    db.close()

    assert "yodak_SUPERSECRET" not in blobs
    assert "topsecret" not in blobs
    assert "hunter2" not in blobs


def test_audit_query_filters(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")

    audit.append(client_id="cl_a", action="mission.created", mission_id="m_1")
    audit.append(client_id="cl_b", action="mission.created", mission_id="m_2")
    audit.append(client_id="cl_a", action="mission.cancel_requested", mission_id="m_1")

    assert len(audit.query(client_id="cl_a")) == 2
    assert len(audit.query(mission_id="m_1")) == 2
    assert len(audit.query(action="mission.cancel_requested")) == 1
    assert len(audit.query()) == 3

    # Order is global append order.
    seqs = [r["seq"] for r in audit.query()]
    assert seqs == sorted(seqs)


def test_admin_endpoints_require_shared_key(monkeypatch):
    monkeypatch.setenv("YODAW_API_KEY", "admin-secret")
    client = TestClient(app)

    # No key -> 401; client keys -> 403; shared key -> 200.
    assert client.post("/api/v1/clients", json={"name": "x"}).status_code == 401

    clients_store = main_module.clients
    created = clients_store.create_client("tenant-a")
    tenant_headers = {
        "Authorization": f"Bearer {created['api_key']}"
    }

    forbidden = client.post(
        "/api/v1/clients",
        json={"name": "escalate"},
        headers=tenant_headers,
    )
    assert forbidden.status_code == 403

    admin_headers = {"Authorization": "Bearer admin-secret"}

    ok = client.post(
        "/api/v1/clients",
        json={"name": "tenant-b", "priority": 3},
        headers=admin_headers,
    )
    assert ok.status_code == 200
    assert ok.json()["api_key"].startswith("yodak_")

    listing = client.get("/api/v1/clients", headers=admin_headers)
    assert listing.status_code == 200
    names = [c["name"] for c in listing.json()]
    assert "tenant-b" in names
    # Listing must not expose key material.
    assert all("api_key" not in c and "key_hash" not in c for c in listing.json())

    priority = client.post(
        "/api/v1/clients/tenant-b/priority",
        json={"priority": 1},
        headers=admin_headers,
    )
    assert priority.status_code == 200
    assert priority.json()["priority"] == 1

    disable = client.post(
        "/api/v1/clients/tenant-b/disable", headers=admin_headers
    )
    assert disable.status_code == 200
    assert clients_store.authenticate(ok.json()["api_key"]) is None

    enable = client.post(
        "/api/v1/clients/tenant-b/enable", headers=admin_headers
    )
    assert enable.status_code == 200
    assert clients_store.authenticate(ok.json()["api_key"]) is not None

    monkeypatch.delenv("YODAW_API_KEY", raising=False)


def test_tenant_mission_carries_identity_priority_and_quota(monkeypatch):
    """End-to-end tenant path: attribution, priority, quota 429."""
    import threading
    import time

    from tests.helpers import poll_mission

    monkeypatch.delenv("YODAW_API_KEY", raising=False)

    release = threading.Event()

    class BlockedWorker:
        name = "tenant-blocked-bud"
        capabilities = {"tenant-quota-cap"}

        def supports(self, capability):
            return capability in self.capabilities

        def health(self):
            return {"status": "READY"}

        def execute(self, goal, metadata=None):
            release.wait(timeout=30)
            return {
                "success": True,
                "output": {},
                "evidence": [],
                "error": None,
            }

    from app.workers.registry import registry

    registry.workers.append(BlockedWorker())

    client = TestClient(app)

    clients_store = main_module.clients
    created = clients_store.create_client(
        "quota-co", priority=1, max_concurrent_missions=1
    )
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    record = clients_store.authenticate(created["api_key"])

    # One mission claims and runs (blocked worker keeps it RUNNING).
    first = client.post(
        "/api/v1/missions",
        json={
            "goal": f"quota e2e {uuid.uuid4().hex}",
            "capability": "tenant-quota-cap",
        },
        headers=headers,
    )
    assert first.status_code == 200

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        stored = main_module.store.get(first.json()["id"])
        if stored.status == "RUNNING":
            break
        time.sleep(0.05)

    assert stored.status == "RUNNING"
    assert stored.client_id == record.id
    assert stored.priority == 1

    # Second concurrent mission for the same client hits the quota.
    second = client.post(
        "/api/v1/missions",
        json={
            "goal": f"quota over {uuid.uuid4().hex}",
            "capability": "tenant-quota-cap",
        },
        headers=headers,
    )
    assert second.status_code == 429

    # A different client (no limit) is admitted while the first is
    # still running: quotas are per client, not global.
    other = clients_store.create_client("other-co", priority=5)
    other_headers = {"Authorization": f"Bearer {other['api_key']}"}

    third = client.post(
        "/api/v1/missions",
        json={
            "goal": f"other client ok {uuid.uuid4().hex}",
            "capability": "tenant-quota-cap",
        },
        headers=other_headers,
    )
    assert third.status_code == 200

    # Audit trail recorded the tenant actions.
    audit_rows = main_module.audit.query(client_id=record.id)
    actions = [r["action"] for r in audit_rows]
    assert "mission.created" in actions
    assert "mission.rejected" in actions
    rejected = next(
        r for r in audit_rows if r["action"] == "mission.rejected"
    )
    assert rejected["data"]["reason"] == "quota_exceeded"

    release.set()


def test_quotas_enforced_race_free_at_claim_time(tmp_path):
    """The claim transaction itself must enforce client limits."""
    from app.core.models import Mission
    from app.storage.sqlite_store import MissionStore

    store = MissionStore(tmp_path / "q.db")

    missions = []
    for i in range(5):
        mission = Mission(
            goal=f"race {i}",
            capability="x",
            client_id="cl_race",
            priority=1,
        )
        store.enqueue(mission)
        missions.append(mission)

    claimed = []
    lock = threading.Lock()

    def claimer():
        while True:
            mission = store.claim_next(
                f"coord-{threading.get_ident()}",
                client_limits={"cl_race": 2},
            )

            if mission is None:
                return

            with lock:
                claimed.append(mission.id)

    threads = [threading.Thread(target=claimer) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Hard quota: exactly 2 of 5 missions may be executing at
    # once. Since the fake claimers never finish their missions,
    # the remaining 3 stay QUEUED forever.
    assert len(claimed) == 2
    assert len(set(claimed)) == 2

    # The unclaimed missions are still queued and intact.
    remaining = [
        m for m in store.list() if m.status.value == "QUEUED"
    ]
    assert len(remaining) == 3


def test_priority_ordering_under_queue_pressure(tmp_path):
    """Lower priority number claims first; FIFO within a class."""
    import datetime as dt

    from app.core.models import Mission
    from app.storage.sqlite_store import MissionStore

    store = MissionStore(tmp_path / "p.db")
    now = dt.datetime.now(dt.timezone.utc)

    # Submitted in reverse priority order with identical timestamps
    # within each class; priority must dominate, FIFO must hold.
    for priority, goal in ((9, "p9-a"), (9, "p9-b"), (3, "p3-a"), (1, "p1-a")):
        mission = Mission(goal=goal, capability="x", priority=priority)
        store.enqueue(mission)

    order = []

    while True:
        mission = store.claim_next("coord-order")

        if mission is None:
            break

        order.append(mission.goal)

    assert order == ["p1-a", "p3-a", "p9-a", "p9-b"]
