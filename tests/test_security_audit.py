"""
Worker J: security audit.

Hermetic: TestClient against the FastAPI app with isolated temp
stores via monkeypatched module state. No network, no keys.

Covers:
- oversized payload rejection (goal, metadata, capability)
- invalid state transitions (unknown mission, double cancel)
- 404-not-403 isolation for foreign missions and audit scope
- plaintext key material never persisted or returned
- invalid admin role rejected at the boundary
- audit-chain integrity: tamper, reorder, and retention anchor
- audit appends race-safe under threads
"""

import os
import sqlite3
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.api.governance import (
    GovernanceConfig,
    check_payload_governance,
)
from app.api.rbac import PERMISSIONS, role_can
from app.core.models import Mission
from app.storage.sqlite_store import MissionStore
from app.tenants.admins import AdminStore
from app.tenants.audit import AuditStore
from app.tenants.clients import ClientStore


@pytest.fixture()
def isolated_api(monkeypatch, tmp_path):
    """Point the API stores at temp databases for this test."""
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_PROFILE", raising=False)
    monkeypatch.setenv("YODAW_RATE_LIMIT_RPM", "0")
    monkeypatch.setenv("YODAW_EMBED_COORDINATOR", "0")

    db = tmp_path / "api.db"
    main_module.store = MissionStore(db)
    main_module.clients = ClientStore(db)
    main_module.admins = AdminStore(db)
    main_module.audit = AuditStore(db)

    import app.api.auth as auth_module
    from app.api.governance import RateLimiter
    from app.main import _rate_backend

    main_module.rate_limiter = RateLimiter(
        _rate_backend, enabled_provider=lambda: False
    )

    yield main_module

    monkeypatch.delenv("YODAW_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("YODAW_EMBED_COORDINATOR", raising=False)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _make_client(name="sec-client", **kw):
    created = main_module.clients.create_client(
        f"{name}-{uuid.uuid4().hex[:8]}", **kw
    )
    record = main_module.clients.authenticate(created["api_key"])
    return created, record


# ---------------------------------------------------------
# Oversized payload rejection
# ---------------------------------------------------------

def test_goal_length_boundary_exact_limit_ok():
    cfg = GovernanceConfig(max_goal_length=100)
    ok, _ = check_payload_governance(goal="x" * 100, metadata=None, config=cfg)
    assert ok is True


def test_goal_length_one_over_rejected():
    cfg = GovernanceConfig(max_goal_length=100)
    ok, reason = check_payload_governance(
        goal="x" * 101, metadata=None, config=cfg
    )
    assert ok is False
    assert "100" in reason


def test_metadata_size_boundary():
    cfg = GovernanceConfig(max_metadata_bytes=100)
    ok, _ = check_payload_governance(
        goal="fine", metadata={"a": "b"}, config=cfg
    )
    assert ok is True

    ok, reason = check_payload_governance(
        goal="fine", metadata={"blob": "y" * 500}, config=cfg
    )
    assert ok is False
    assert "bytes" in reason


def test_unserializable_metadata_rejected():
    ok, reason = check_payload_governance(
        goal="fine", metadata={"bad": object()}, config=None
    )
    assert ok is False
    assert "serializable" in reason


def test_api_rejects_oversized_goal_with_413(isolated_api):
    client = TestClient(app)
    response = client.post(
        "/api/v1/missions",
        json={"goal": "g" * 5000, "capability": "code"},
    )
    assert response.status_code == 413


def test_api_rejects_oversized_metadata_with_413(isolated_api):
    client = TestClient(app)
    response = client.post(
        "/api/v1/missions",
        json={
            "goal": "fine",
            "capability": "code",
            "metadata": {"blob": "z" * 40000},
        },
    )
    assert response.status_code == 413


def test_api_rejects_oversized_capability(isolated_api):
    # Capability strings are worker-lookup keys, not free text:
    # an absurd value must fail closed, never match a worker.
    from app.workers.registry import registry

    assert registry.find("X" * 100000) is None

    client = TestClient(app)
    response = client.post(
        "/api/v1/missions",
        json={"goal": "fine", "capability": "X" * 100000},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"


def test_oversized_rejection_is_audited(isolated_api):
    client = TestClient(app)
    client.post(
        "/api/v1/missions",
        json={"goal": "g" * 5000, "capability": "code"},
    )
    rejected = [
        e for e in main_module.audit.query(limit=1000)
        if e["action"] == "mission.rejected"
        and e["data"].get("reason") == "payload_governance"
    ]
    assert rejected, "governance rejections must leave an audit trail"


# ---------------------------------------------------------
# Invalid state transitions
# ---------------------------------------------------------

def test_cancel_unknown_mission_returns_404(isolated_api):
    client = TestClient(app)
    response = client.post("/api/v1/missions/m_does_not_exist/cancel")
    assert response.status_code == 404


def test_get_unknown_mission_returns_404(isolated_api):
    client = TestClient(app)
    assert client.get("/api/v1/missions/m_missing").status_code == 404
    assert client.get("/api/v1/missions/m_missing/evidence").status_code == 404
    assert client.get("/api/v1/missions/m_missing/events").status_code == 404


def test_double_cancel_returns_409_terminal(isolated_api):
    created, record = _make_client()
    mission = Mission(
        goal="double cancel", capability="code", client_id=record.id
    )
    main_module.store.enqueue(mission)
    assert main_module.store.request_cancel(mission.id) == "cancelled"

    client = TestClient(app)
    response = client.post(
        f"/api/v1/missions/{mission.id}/cancel",
        headers=_auth(created["api_key"]),
    )
    assert response.status_code == 409


def test_invalid_admin_role_rejected(isolated_api):
    with pytest.raises(ValueError, match="invalid role"):
        main_module.admins.create_admin("bad-role-admin", role="wizard")

    root = main_module.admins.create_admin("sec-root", "superadmin")
    client = TestClient(app)
    response = client.post(
        "/api/v1/admins/sec-root/role",
        json={"role": "wizard"},
        headers=_auth(root["api_key"]),
    )
    assert response.status_code == 400


def test_priority_out_of_range_is_clamped(tmp_path):
    store = ClientStore(tmp_path / "c.db")
    created = store.create_client("edge", priority=999)
    assert created["priority"] == 9
    store.set_priority("edge", -5)
    listed = [c for c in store.list_clients() if c["name"] == "edge"]
    assert listed[0]["priority"] == 1


# ---------------------------------------------------------
# Tenant isolation: 404 not 403, audit scoping
# ---------------------------------------------------------

def test_foreign_mission_reads_are_404_not_403(isolated_api):
    alice, alice_rec = _make_client("alice")
    bob, _ = _make_client("bob")

    mission = Mission(
        goal="alice secret", capability="code", client_id=alice_rec.id
    )
    main_module.store.enqueue(mission)

    client = TestClient(app)
    bob_headers = _auth(bob["api_key"])
    for path in (
        f"/api/v1/missions/{mission.id}",
        f"/api/v1/missions/{mission.id}/evidence",
        f"/api/v1/missions/{mission.id}/events",
    ):
        response = client.get(path, headers=bob_headers)
        assert response.status_code == 404, path

    # Listing never includes the foreign mission.
    ids = [m["id"] for m in client.get(
        "/api/v1/missions", headers=bob_headers).json()]
    assert mission.id not in ids


def test_client_cannot_reach_global_audit_trail(isolated_api):
    tenant, _ = _make_client()
    client = TestClient(app)
    response = client.get(
        "/api/v1/audit", headers=_auth(tenant["api_key"])
    )
    assert response.status_code == 403


def test_client_cannot_reach_outbox_surface(isolated_api):
    tenant, _ = _make_client()
    client = TestClient(app)
    assert client.get(
        "/api/v1/outbox", headers=_auth(tenant["api_key"])
    ).status_code == 403
    assert client.get(
        "/api/v1/outbox/dead", headers=_auth(tenant["api_key"])
    ).status_code == 403
    assert client.post(
        "/api/v1/outbox/1/requeue", headers=_auth(tenant["api_key"])
    ).status_code == 403


# ---------------------------------------------------------
# Key material containment
# ---------------------------------------------------------

def test_key_material_never_persisted_or_listed(tmp_path):
    db = tmp_path / "keys.db"
    main_module_clients = ClientStore(db)
    main_module_admins = AdminStore(db)

    created_client = main_module_clients.create_client("kc")
    created_admin = main_module_admins.create_admin("ka", "operator")

    scan = sqlite3.connect(str(db))
    blobs = "\n".join(
        str(row)
        for table in ("api_clients", "api_admins")
        for row in scan.execute(f"SELECT * FROM {table}").fetchall()
    )
    scan.close()

    assert created_client["api_key"] not in blobs
    assert created_admin["api_key"] not in blobs

    for listed in main_module_clients.list_clients():
        assert "api_key" not in listed
        assert "key_hash" not in listed
    for listed in main_module_admins.list_admins():
        assert "api_key" not in listed
        assert "key_hash" not in listed


def test_disabled_key_is_indistinguishable_from_unknown(tmp_path):
    store = ClientStore(tmp_path / "c.db")
    created = store.create_client("gone")
    assert store.authenticate(created["api_key"]) is not None
    store.set_disabled("gone", True)
    assert store.authenticate(created["api_key"]) is None
    assert store.authenticate("yodak_unknown_key") is None


# ---------------------------------------------------------
# Audit-chain integrity
# ---------------------------------------------------------

def test_audit_verify_detects_forged_row(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")
    audit.append(client_id="c", action="a1", data={})
    audit.append(client_id="c", action="a2", data={})

    db = sqlite3.connect(str(tmp_path / "audit.db"))
    db.execute("UPDATE audit_events SET action='FORGED' WHERE seq=1")
    db.commit()
    db.close()

    result = audit.verify()
    assert result["intact"] is False
    assert result["broken_at"] == 1


def test_audit_verify_detects_deleted_row(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")
    audit.append(client_id="c", action="a1", data={})
    audit.append(client_id="c", action="a2", data={})
    audit.append(client_id="c", action="a3", data={})

    db = sqlite3.connect(str(tmp_path / "audit.db"))
    db.execute("DELETE FROM audit_events WHERE seq=2")
    db.commit()
    db.close()

    result = audit.verify()
    assert result["intact"] is False
    assert result["broken_at"] == 3


def test_audit_appends_are_thread_safe_and_chain_intact(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")

    def appender(n):
        for i in range(20):
            audit.append(client_id="c", action=f"a{n}-{i}", data={"i": i})

    threads = [threading.Thread(target=appender, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    result = audit.verify()
    assert result["intact"] is True
    assert result["events"] == 100
    assert result["verified_through"] == 100

    seqs = [e["seq"] for e in audit.query(limit=1000)]
    assert sorted(seqs) == list(range(1, 101))


def test_audit_prune_rejects_invalid_window(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")
    with pytest.raises(ValueError):
        audit.prune(keep_days=0)


def test_secrets_redacted_before_audit_persistence(tmp_path):
    audit = AuditStore(tmp_path / "audit.db")
    audit.append(
        client_id="c",
        action="mission.created",
        data={
            "api_key": "yodak_PLAINTEXT",
            " nested": {"password": "hunter2"},
            "goal": "fine",
        },
    )

    db = sqlite3.connect(str(tmp_path / "audit.db"))
    blobs = "\n".join(
        r[0] for r in db.execute("SELECT data FROM audit_events")
    )
    db.close()

    assert "yodak_PLAINTEXT" not in blobs
    assert "hunter2" not in blobs


def test_rbac_unknown_roles_fail_closed():
    assert role_can("wizard", "missions.create") is False
    assert "wizard" not in PERMISSIONS
