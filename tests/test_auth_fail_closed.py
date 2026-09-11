"""
P0 authentication fail-closed regression coverage.

Authentication must deny — never silently widen — whenever the
deployment is not an explicitly unauthenticated local-open
development server. The regression this file guards is a resolver
that granted an anonymous superadmin to any request whose bearer
key was missing, empty, unknown, or revoked, because it only
inspected ``YODAW_API_KEY`` and ignored every other credential
surface.

Contracts proven here:

- local-open anonymous superadmin requires the ``local`` profile
  with no admin identity and no shared key configured;
- a missing, empty, malformed, unknown, or revoked credential is
  denied ``401``;
- a non-local profile can never resolve an anonymous principal;
- an unreadable identity store denies ``503`` instead of falling
  through to the anonymous principal;
- the shared key and admin identities keep working, and key
  rotation really does invalidate the retired key.

Everything is hermetic: temp stores, no network, no providers.
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from app.api.auth import (
    admin_credential_surfaces,
    current_profile,
    open_dev_mode,
    resolve_principal,
)
from app.storage.sqlite_store import MissionStore
from app.tenants.admins import AdminStore
from app.tenants.audit import AuditStore
from app.tenants.clients import ClientStore


@pytest.fixture()
def isolated_api(monkeypatch, tmp_path):
    """Bind the API's stores to temp databases for this test."""
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_PROFILE", raising=False)
    monkeypatch.setenv("YODAW_RATE_LIMIT_RPM", "0")
    monkeypatch.setenv("YODAW_EMBED_COORDINATOR", "0")

    db = tmp_path / "api.db"
    monkeypatch.setattr(main_module, "store", MissionStore(db))
    monkeypatch.setattr(main_module, "clients", ClientStore(db))
    monkeypatch.setattr(main_module, "admins", AdminStore(db))
    monkeypatch.setattr(main_module, "audit", AuditStore(db))

    from app.api.governance import RateLimiter
    from app.main import _rate_backend

    monkeypatch.setattr(
        main_module,
        "rate_limiter",
        RateLimiter(_rate_backend, enabled_provider=lambda: False),
    )

    yield main_module

    monkeypatch.delenv("YODAW_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("YODAW_EMBED_COORDINATOR", raising=False)


def _auth(key):
    return {"Authorization": f"Bearer {key}"}


def _submit(client, headers=None, goal=None):
    return client.post(
        "/api/v1/missions",
        json={
            "goal": goal or f"fail-closed probe {uuid.uuid4().hex}",
            "capability": "code",
        },
        headers=headers,
    )


class _ExplodingAdminStore:
    """An admin store that cannot be read at all."""

    def authenticate(self, plaintext_key):
        raise RuntimeError("admin store unavailable")

    def count_admins(self):
        raise RuntimeError("admin store unavailable")


class _EmptyClientStore:
    def authenticate(self, plaintext_key):
        return None

    def count_clients(self):
        return 0


# ---------------------------------------------------------
# Local-open development mode is narrow and explicit
# ---------------------------------------------------------

def test_anonymous_allowed_only_in_bare_local_open_mode(isolated_api):
    """No identity surface + local profile = documented open mode."""
    client = TestClient(app)

    response = _submit(client)

    assert response.status_code == 200
    assert response.json()["status"] in ("QUEUED", "EXECUTING", "PASS")

    principal = resolve_principal(None)

    assert principal.kind == "local-dev"
    assert principal.role == "superadmin"


def test_missing_credentials_denied_once_an_admin_exists(isolated_api):
    """The instant one admin identity exists, anonymous is denied."""
    main_module.admins.create_admin("p0-root", role="superadmin")

    client = TestClient(app)

    assert _submit(client).status_code == 401
    assert client.get("/api/v1/missions").status_code == 401

    with pytest.raises(HTTPException) as excinfo:
        resolve_principal(None)

    assert excinfo.value.status_code == 401


def test_empty_and_whitespace_bearer_denied(isolated_api):
    main_module.admins.create_admin("p0-root-empty", role="superadmin")

    client = TestClient(app)

    assert _submit(client, headers={"Authorization": "Bearer "}).status_code == 401
    assert _submit(client, headers={"Authorization": "Bearer"}).status_code == 401
    assert (
        _submit(client, headers={"Authorization": "Basic abc"}).status_code
        == 401
    )


def test_invalid_credentials_denied_once_an_admin_exists(isolated_api):
    main_module.admins.create_admin("p0-root-invalid", role="superadmin")

    client = TestClient(app)

    assert (
        _submit(client, headers=_auth("yodad_not-a-real-key")).status_code
        == 401
    )


def test_revoked_credentials_denied(isolated_api):
    created = main_module.admins.create_admin("p0-root-revoked", role="superadmin")

    client = TestClient(app)

    assert _submit(client, headers=_auth(created["api_key"])).status_code == 200

    main_module.admins.set_disabled("p0-root-revoked", True)

    assert _submit(client, headers=_auth(created["api_key"])).status_code == 401
    # And the revoked credential must not downgrade into anonymous.
    assert _submit(client).status_code == 401


def test_nonlocal_profiles_can_never_resolve_anonymous(isolated_api, monkeypatch):
    """Every non-local profile denies an anonymous caller outright,
    even with no identity configured at all."""
    ctx = type(
        "Ctx",
        (),
        {"admins": main_module.admins, "clients": main_module.clients},
    )()

    for profile in ("single-node", "multi-process", "production"):
        monkeypatch.setenv("YODAW_PROFILE", profile)

        assert current_profile() == profile
        # The profile gate is evaluated before any store is read, so
        # an empty identity store cannot open the anonymous path.
        assert open_dev_mode(ctx) is False

        with pytest.raises(HTTPException) as excinfo:
            resolve_principal(None, admins=ctx.admins, clients=ctx.clients)

        assert excinfo.value.status_code == 401

    monkeypatch.delenv("YODAW_PROFILE", raising=False)


# ---------------------------------------------------------
# Fail closed on store failure
# ---------------------------------------------------------

def test_unreadable_auth_store_denies_503(isolated_api):
    """An unreadable store must deny, not fall through to local-dev."""
    with pytest.raises(HTTPException) as excinfo:
        resolve_principal(
            None,
            admins=_ExplodingAdminStore(),
            clients=_EmptyClientStore(),
        )

    assert excinfo.value.status_code == 503

    with pytest.raises(HTTPException) as excinfo:
        resolve_principal(
            "Bearer anything",
            admins=_ExplodingAdminStore(),
            clients=_EmptyClientStore(),
        )

    assert excinfo.value.status_code == 503


def test_unreadable_auth_store_surfaces_as_503_over_http(
    isolated_api, monkeypatch
):
    monkeypatch.setattr(
        main_module, "admins", _ExplodingAdminStore()
    )
    monkeypatch.setattr(main_module, "clients", _EmptyClientStore())

    client = TestClient(app)

    assert _submit(client).status_code == 503


# ---------------------------------------------------------
# Legitimate credentials still work
# ---------------------------------------------------------

def test_shared_key_authenticates_and_other_keys_do_not(
    isolated_api, monkeypatch
):
    monkeypatch.setenv("YODAW_API_KEY", "shared-secret")

    client = TestClient(app)

    assert _submit(client).status_code == 401
    assert _submit(client, headers=_auth("wrong")).status_code == 401

    response = _submit(client, headers=_auth("shared-secret"))

    assert response.status_code == 200


def test_admin_key_rotation_invalidates_the_retired_key(isolated_api):
    created = main_module.admins.create_admin("p0-rotate", role="superadmin")

    client = TestClient(app)

    assert _submit(client, headers=_auth(created["api_key"])).status_code == 200

    rotated = main_module.admins.rotate_key("p0-rotate")

    assert rotated["api_key"] != created["api_key"]
    assert _submit(client, headers=_auth(created["api_key"])).status_code == 401
    assert _submit(client, headers=_auth(rotated["api_key"])).status_code == 200


# ---------------------------------------------------------
# Admin-surface semantics (bootstrap must stay reachable)
# ---------------------------------------------------------

def test_admin_credential_surfaces_reports_only_admin_credentials(
    isolated_api,
):
    ctx = type(
        "Ctx",
        (),
        {"admins": main_module.admins, "clients": main_module.clients},
    )()

    assert admin_credential_surfaces(ctx) == ()
    assert open_dev_mode(ctx) is True

    # A tenant identity is not an admin credential: treating it as
    # one would strand a fresh local deployment with no way back
    # into the admin surface (there is no CLI admin bootstrap).
    main_module.clients.create_client("tenant-only")

    assert admin_credential_surfaces(ctx) == ()
    assert open_dev_mode(ctx) is True

    # The admin credential is what closes the anonymous path.
    main_module.admins.create_admin("p0-ctx-root", role="superadmin")

    assert admin_credential_surfaces(ctx) == ("admins",)
    assert open_dev_mode(ctx) is False
