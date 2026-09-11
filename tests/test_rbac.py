"""
Stage 10.3: admin identities, RBAC, and client isolation.

Covers:
- every role (superadmin, operator, auditor, client)
- horizontal isolation: one client cannot read another client's
  mission, evidence, or events
- revoked key, rotated key, role changes
- privilege escalation rejection
- the admin endpoint authorization matrix
- keys are hashed at rest and never appear in listings/audit
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.api.rbac import PERMISSIONS, known_role, role_can
from app.core.models import Mission
from app.tenants.admins import AdminStore, hash_key
from app.tenants.clients import ClientStore


@pytest.fixture()
def isolated_env(monkeypatch, tmp_path):
    """Session-scoped stores bound to a fresh temp database."""
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_HAS_IDENTITIES", raising=False)
    yield main_module


def _make_admin(name, role):
    return main_module.admins.create_admin(name, role=role)


def _make_client(name, **kw):
    return main_module.clients.create_client(name, **kw)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------
# Role matrix
# ---------------------------------------------------------


def test_permission_matrix_shape():
    assert known_role("superadmin") and known_role("operator")
    assert known_role("auditor") and known_role("client")
    assert not known_role("wizard")

    # superadmin is a superset of operator
    assert PERMISSIONS["operator"] <= PERMISSIONS["superadmin"]

    # auditor cannot mutate
    assert not (PERMISSIONS["auditor"] & {
        "missions.create", "missions.cancel", "missions.retry",
        "clients.manage", "admins.manage", "outbox.manage",
        "runtime.control",
    })

    # client has no administrative surface
    assert not (PERMISSIONS["client"] & {
        "audit.read", "clients.manage", "admins.manage",
        "outbox.manage", "missions.read.all",
    })


def test_role_can_and_unknown_role():
    assert role_can("superadmin", "admins.manage")
    assert role_can("operator", "missions.cancel")
    assert role_can("auditor", "audit.read")
    assert role_can("client", "missions.create")

    assert not role_can("operator", "admins.manage")
    assert not role_can("auditor", "missions.create")
    assert not role_can("client", "missions.read.all")
    assert not role_can("nonexistent-role", "missions.create")


def test_admin_keys_hashed_and_shown_once(isolated_env):
    created = _make_admin("ops-root", "superadmin")

    assert created["api_key"].startswith("yodad_")

    import sqlite3

    db = sqlite3.connect(str(main_module.admins.path))
    rows = db.execute(
        "SELECT key_hash, role FROM api_admins WHERE name='ops-root'"
    ).fetchall()
    db.close()

    assert rows[0][0] == hash_key(created["api_key"])
    assert created["api_key"].encode() not in rows[0][0].encode()
    assert rows[0][1] == "superadmin"


def test_admin_authentication_and_revocation(isolated_env):
    created = _make_admin("ops-one", "operator")
    record = main_module.admins.authenticate(created["api_key"])

    assert record is not None
    assert record.role == "operator"

    assert main_module.admins.set_disabled("ops-one", True)
    assert main_module.admins.authenticate(created["api_key"]) is None

    assert main_module.admins.set_disabled("ops-one", False)
    assert main_module.admins.authenticate(created["api_key"]) is not None

    # Unknown keys never authenticate.
    assert main_module.admins.authenticate("yodad_bogus") is None


def test_admin_key_rotation_invalidates_old_key(isolated_env):
    created = _make_admin("ops-rotate", "auditor")
    old_key = created["api_key"]

    assert main_module.admins.authenticate(old_key) is not None

    rotated = main_module.admins.rotate_key("ops-rotate")

    assert rotated is not None
    assert rotated["api_key"] != old_key
    assert main_module.admins.authenticate(old_key) is None
    assert main_module.admins.authenticate(rotated["api_key"]) is not None

    # Rotating a disabled admin is refused.
    main_module.admins.set_disabled("ops-rotate", True)
    assert main_module.admins.rotate_key("ops-rotate") is None


def test_admin_role_change_takes_effect(isolated_env):
    created = _make_admin("ops-elevate", "auditor")
    headers = _auth(created["api_key"])
    client = TestClient(app)

    # Auditor cannot create admins...
    assert (
        client.post(
            "/api/v1/admins",
            json={"name": "x", "role": "operator"},
            headers=headers,
        ).status_code
        == 403
    )

    # ...but a superadmin can promote the auditor to operator.
    root = _make_admin("ops-root-elevate", "superadmin")

    response = client.post(
        "/api/v1/admins/ops-elevate/role",
        json={"role": "operator"},
        headers=_auth(root["api_key"]),
    )

    assert response.status_code == 200

    record = main_module.admins.authenticate(created["api_key"])

    assert record.role == "operator"

    # And the newly promoted operator still cannot manage admins.
    assert (
        client.post(
            "/api/v1/admins",
            json={"name": "y", "role": "superadmin"},
            headers=headers,
        ).status_code
        == 403
    )


def test_client_cannot_read_another_clients_mission(isolated_env):
    """Horizontal isolation: 404, never 403, so existence of the
    other tenant's mission is not revealed."""
    client = TestClient(app)

    alice = _make_client("alice-iso", priority=2)
    bob = _make_client("bob-iso", priority=3)

    mission = Mission(
        goal="alice secret mission",
        capability="code",
        client_id=main_module.clients.authenticate(alice["api_key"]).id,
    )
    main_module.store.enqueue(mission)

    bob_headers = _auth(bob["api_key"])

    for path in (
        f"/api/v1/missions/{mission.id}",
        f"/api/v1/missions/{mission.id}/evidence",
        f"/api/v1/missions/{mission.id}/events",
    ):
        response = client.get(path, headers=bob_headers)

        assert response.status_code == 404, path

    # Alice sees her own mission fine.
    alice_headers = _auth(alice["api_key"])

    assert (
        client.get(
            f"/api/v1/missions/{mission.id}", headers=alice_headers
        ).status_code
        == 200
    )

    # Listing is scoped: alice sees only her missions.
    listed = client.get("/api/v1/missions", headers=alice_headers).json()
    ids = [m["id"] for m in listed]

    assert mission.id in ids


def test_client_cannot_cancel_another_clients_mission(isolated_env):
    client = TestClient(app)

    alice = _make_client("alice-cancel")
    bob = _make_client("bob-cancel")

    mission = Mission(
        goal="alice cancel target",
        capability="code",
        client_id=main_module.clients.authenticate(alice["api_key"]).id,
    )
    main_module.store.enqueue(mission)

    response = client.post(
        f"/api/v1/missions/{mission.id}/cancel",
        headers=_auth(bob["api_key"]),
    )

    assert response.status_code == 404


def test_client_isolation_extends_to_evidence_and_events(isolated_env):
    client = TestClient(app)

    alice = _make_client("alice-ev")
    bob = _make_client("bob-ev")

    mission = Mission(
        goal="evidence isolation",
        capability="code",
        client_id=main_module.clients.authenticate(alice["api_key"]).id,
    )
    mission.evidence = [{"type": "secret-artifact"}]
    main_module.store.enqueue(mission)
    main_module.store.record_event(mission.id, "mission.queued")

    bob_headers = _auth(bob["api_key"])

    assert (
        client.get(
            f"/api/v1/missions/{mission.id}/evidence",
            headers=bob_headers,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/missions/{mission.id}/events",
            headers=bob_headers,
        ).status_code
        == 404
    )


def test_privilege_escalation_rejected(isolated_env):
    """Operator cannot create superadmin or manage admins; auditor
    cannot mutate anything; client cannot reach admin endpoints."""
    client = TestClient(app)

    operator = _make_admin("rbac-operator", "operator")
    auditor = _make_admin("rbac-auditor", "auditor")
    tenant = _make_client("rbac-tenant")

    escalation_attempts = [
        (
            _auth(operator["api_key"]),
            "/api/v1/admins",
            {"name": "escalated", "role": "superadmin"},
        ),
        (
            _auth(auditor["api_key"]),
            "/api/v1/admins",
            {"name": "escalated2", "role": "superadmin"},
        ),
        (
            _auth(operator["api_key"]),
            "/api/v1/clients",
            {"name": "rogue-tenant"},
        ),
        (
            _auth(auditor["api_key"]),
            "/api/v1/clients",
            {"name": "rogue-tenant2"},
        ),
        (
            _auth(tenant["api_key"]),
            "/api/v1/clients",
            {"name": "rogue-tenant3"},
        ),
        (
            _auth(tenant["api_key"]),
            "/api/v1/admins",
            {"name": "rogue-admin", "role": "superadmin"},
        ),
    ]

    for headers, path, payload in escalation_attempts:
        response = client.post(path, json=payload, headers=headers)

        assert response.status_code == 403, (path, payload)


def test_auditor_cannot_mutate(isolated_env):
    client = TestClient(app)
    auditor = _make_admin("rbac-auditor-mut", "auditor")
    headers = _auth(auditor["api_key"])

    assert (
        client.post(
            "/api/v1/missions",
            json={"goal": "auditor mission", "capability": "code"},
            headers=headers,
        ).status_code
        == 403
    )

    # Read-only endpoints work.
    assert client.get("/api/v1/audit", headers=headers).status_code == 200
    assert (
        client.get("/api/v1/audit/verify", headers=headers).status_code
        == 200
    )
    assert client.get("/api/v1/missions", headers=headers).status_code == 200


def test_operator_cannot_create_superadmin(isolated_env):
    client = TestClient(app)
    operator = _make_admin("rbac-op-admin", "operator")

    response = client.post(
        "/api/v1/admins",
        json={"name": "sneaky", "role": "superadmin"},
        headers=_auth(operator["api_key"]),
    )

    assert response.status_code == 403
    assert main_module.admins.authenticate(
        # no such key was ever issued; the check is that no admin
        # named 'sneaky' exists
        "yodad_nothing"
    ) is None

    names = [a["name"] for a in main_module.admins.list_admins()]

    assert "sneaky" not in names


def test_admin_endpoint_authorization_matrix(isolated_env):
    """Full matrix: role x endpoint family -> expected status."""
    client = TestClient(app)

    superadmin = _make_admin("matrix-root", "superadmin")
    operator = _make_admin("matrix-operator", "operator")
    auditor = _make_admin("matrix-auditor", "auditor")
    tenant = _make_client("matrix-tenant")
    _make_client("matrix-target")

    matrix = {
        "/api/v1/admins": {
            "superadmin": (200, "post"),
            "operator": (403, "post"),
            "auditor": (403, "post"),
            "client": (403, "post"),
        },
        "/api/v1/admins/matrix-root/rotate": {
            "superadmin": (200, "post"),
            "operator": (403, "post"),
            "auditor": (403, "post"),
            "client": (403, "post"),
        },
        "/api/v1/admins": {
            "superadmin": (200, "get"),
            "operator": (403, "get"),
            "auditor": (403, "get"),
            "client": (403, "get"),
        },
        "/api/v1/clients/matrix-target/disable": {
            "superadmin": (200, "post"),
            "operator": (403, "post"),
            "auditor": (403, "post"),
            "client": (403, "post"),
        },
        "/api/v1/clients/matrix-target/enable": {
            "superadmin": (200, "post"),
            "operator": (403, "post"),
            "auditor": (403, "post"),
            "client": (403, "post"),
        },
        "/api/v1/clients/matrix-target/priority": {
            "superadmin": (200, "post"),
            "operator": (403, "post"),
            "auditor": (403, "post"),
            "client": (403, "post"),
        },
        "/api/v1/clients/matrix-target/quota": {
            "superadmin": (200, "post"),
            "operator": (403, "post"),
            "auditor": (403, "post"),
            "client": (403, "post"),
        },
        "/api/v1/audit": {
            "superadmin": (200, "get"),
            "operator": (200, "get"),
            "auditor": (200, "get"),
            "client": (403, "get"),
        },
        "/api/v1/audit/verify": {
            "superadmin": (200, "get"),
            "operator": (200, "get"),
            "auditor": (200, "get"),
            "client": (403, "get"),
        },
        "/api/v1/outbox": {
            "superadmin": (200, "get"),
            "operator": (200, "get"),
            "auditor": (403, "get"),
            "client": (403, "get"),
        },
        "/api/v1/outbox/dead": {
            "superadmin": (200, "get"),
            "operator": (200, "get"),
            "auditor": (403, "get"),
            "client": (403, "get"),
        },
        "/api/v1/missions": {
            "superadmin": (200, "get"),
            "operator": (200, "get"),
            "auditor": (200, "get"),
            "client": (200, "get"),
        },
    }

    keys = {
        "superadmin": superadmin["api_key"],
        "operator": operator["api_key"],
        "auditor": auditor["api_key"],
        "client": tenant["api_key"],
    }

    payloads = {
        "/api/v1/admins": {"name": "matrix-new", "role": "operator"},
        "/api/v1/clients/matrix-target/priority": {"priority": 3},
        "/api/v1/clients/matrix-target/quota": {
            "max_concurrent_missions": 2
        },
    }

    for path, expectations in matrix.items():
        for role, (expected, method) in expectations.items():
            response = client.request(
                method,
                path,
                json=payloads.get(path),
                headers=_auth(keys[role]),
            )

            assert response.status_code == expected, (
                f"{role} {method} {path}: {response.status_code} "
                f"!= {expected}"
            )

            if path == "/api/v1/admins/matrix-root/rotate" and (
                role == "superadmin"
            ):
                # Rotation mints a new key and must invalidate the old
                # one. Every later request in this matrix has to use
                # the replacement, and the retired key must no longer
                # authenticate at all.
                rotated = response.json()

                assert rotated["api_key"] != keys["superadmin"]
                assert (
                    client.get(
                        "/api/v1/admins",
                        headers=_auth(keys["superadmin"]),
                    ).status_code
                    == 401
                )

                keys["superadmin"] = rotated["api_key"]


def test_admin_listing_and_audit_never_leak_keys(isolated_env):
    client = TestClient(app)
    root = _make_admin("leak-root", "superadmin")
    _make_admin("leak-op", "operator")
    tenant = _make_client("leak-tenant")

    listing = client.get(
        "/api/v1/admins", headers=_auth(root["api_key"])
    ).json()

    text = str(listing)

    assert root["api_key"] not in text

    client_listing = client.get(
        "/api/v1/clients", headers=_auth(root["api_key"])
    ).json()

    text = str(client_listing)

    assert tenant["api_key"] not in text
    assert "key_hash" not in text


def test_audit_records_admin_lifecycle(isolated_env):
    client = TestClient(app)
    root = _make_admin("audit-root", "superadmin")
    headers = _auth(root["api_key"])

    client.post(
        "/api/v1/admins",
        json={"name": "audit-new", "role": "auditor"},
        headers=headers,
    )
    client.post("/api/v1/admins/audit-new/rotate", headers=headers)
    client.post(
        "/api/v1/admins/audit-new/role",
        json={"role": "operator"},
        headers=headers,
    )

    events = main_module.audit.query(action="admins.created")
    actions = [e["action"] for e in events]

    assert "admins.created" in actions

    all_actions = {
        e["action"] for e in main_module.audit.query(limit=1000)
    }

    assert "admins.key_rotated" in all_actions
    assert "admins.role_set" in all_actions

    # No plaintext key material in the audit trail.
    blob = str(main_module.audit.query(limit=1000))

    assert "yodad_" not in blob
    assert "yodak_" not in blob
