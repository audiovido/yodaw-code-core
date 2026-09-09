"""
Stage 10.3: unified authentication for the API.

Resolution order for the Authorization: Bearer key:

1. registered admin identity   -> AdminPrincipal (RBAC role)
2. registered client identity  -> ClientPrincipal (tenancy scope)
3. shared environment key      -> SharedKeyPrincipal
                                  (virtual superadmin; Stage 8
                                  migration path, deprecated)
4. no key configured at all    -> LocalDevPrincipal
                                  (local-dev open mode; refused
                                  under the production profile)

Principals expose `role` and `permissions`; endpoint guards check
RBAC permissions, and mission reads apply client isolation based
on the principal kind.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass, field

from fastapi import Header, HTTPException

from app.api.rbac import PERMISSIONS, role_can
from app.storage.sqlite_store import MissionStore
from app.tenants.admins import AdminStore
from app.tenants.clients import ClientStore


@dataclass
class Principal:
    """One authenticated caller."""

    kind: str                 # admin | client | shared-key | local-dev
    name: str
    role: str                 # superadmin | operator | auditor | client
    client_id: str | None = None
    admin_id: str | None = None
    priority: int = 5
    max_concurrent_missions: int | None = None
    _permissions: set[str] = field(default_factory=set)

    def can(self, permission: str) -> bool:
        return permission in self._permissions

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "name": self.name,
            "role": self.role,
            "client_id": self.client_id,
        }


@dataclass
class AuthContext:
    """Request-scoped auth collaborators (stores)."""

    admins: AdminStore
    clients: ClientStore


def _stores(
    admins: AdminStore | None,
    clients: ClientStore | None,
) -> AuthContext:
    from app.main import admins as default_admins
    from app.main import clients as default_clients

    return AuthContext(
        admins=admins or default_admins,
        clients=clients or default_clients,
    )


def shared_api_key() -> str:
    return os.environ.get("YODAW_API_KEY", "")


def resolve_principal(
    authorization: str | None,
    admins: AdminStore | None = None,
    clients: ClientStore | None = None,
) -> Principal:
    """Resolve one bearer key to a Principal; 401 on failure."""
    ctx = _stores(admins, clients)

    provided = ""
    if authorization and authorization.startswith("Bearer "):
        provided = authorization[len("Bearer "):]

    if provided:
        admin = ctx.admins.authenticate(provided)

        if admin is not None:
            return Principal(
                kind="admin",
                name=admin.name,
                role=admin.role,
                admin_id=admin.id,
                _permissions=set(PERMISSIONS.get(admin.role, set())),
            )

        client = ctx.clients.authenticate(provided)

        if client is not None:
            return Principal(
                kind="client",
                name=client.name,
                role="client",
                client_id=client.id,
                priority=client.priority,
                max_concurrent_missions=(
                    client.max_concurrent_missions
                ),
                _permissions=set(PERMISSIONS.get("client", set())),
            )

    expected = shared_api_key()

    if not expected:
        # Local dev mode: open access without an identity.
        return Principal(
            kind="local-dev",
            name="local-dev",
            role="superadmin",
            _permissions=set(PERMISSIONS["superadmin"]),
        )

    if provided and hmac.compare_digest(provided, expected):
        # Stage 8 shared key maps to a virtual superadmin. The
        # identity is not in the admin table and cannot be listed
        # or rotated there; migration path is admin identities.
        return Principal(
            kind="shared-key",
            name="shared-key",
            role="superadmin",
            _permissions=set(PERMISSIONS["superadmin"]),
        )

    raise HTTPException(
        status_code=401,
        detail="invalid or missing API key",
    )


def require(
    permission: str | None = None,
    *,
    allow_roles: set[str] | None = None,
):
    """
    FastAPI dependency factory.

    require("audit.read") — permission gate
    require(allow_roles={"superadmin"}) — exact-role gate (used
    where even another privileged role must not pass, e.g. admin
    management is superadmin-only).
    """

    def dependency(
        authorization: str | None = Header(default=None),
    ) -> Principal:
        principal = resolve_principal(authorization)

        if permission is not None and not principal.can(permission):
            raise HTTPException(
                status_code=403,
                detail=f"missing permission: {permission}",
            )

        if allow_roles is not None and principal.role not in allow_roles:
            raise HTTPException(
                status_code=403,
                detail="insufficient role for this operation",
            )

        return principal

    return dependency
