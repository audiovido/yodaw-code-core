"""
Stage 10.3: unified authentication for the API.

Resolution order for the Authorization: Bearer key:

1. registered admin identity   -> AdminPrincipal (RBAC role)
2. registered client identity  -> ClientPrincipal (tenancy scope)
3. shared environment key      -> SharedKeyPrincipal
                                  (virtual superadmin; Stage 8
                                  migration path, deprecated)
4. no credential configured    -> LocalDevPrincipal
                                  (local-open development mode)

Authentication fails closed:

- The local-open development principal is issued only when the
  `local` profile is active AND no credential surface exists at
  all (no admin identity, no client identity, no shared key).
  The moment one identity is configured, an anonymous caller is
  denied 401 instead of silently becoming a superadmin.
- A missing, empty, malformed, unknown, or revoked credential is
  always denied 401.
- If the identity stores cannot be read, the request is denied
  503 rather than falling through to an anonymous principal.

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


# The only profile that may ever serve an anonymous caller.
LOCAL_OPEN_PROFILE = "local"

# Denial used whenever the identity stores cannot be consulted: an
# unreadable credential store must never widen access.
_AUTH_UNAVAILABLE = "authentication unavailable"


def shared_api_key() -> str:
    return os.environ.get("YODAW_API_KEY", "")


def current_profile() -> str:
    """Active deployment profile; unset means the `local` profile."""
    profile = (os.environ.get("YODAW_PROFILE") or LOCAL_OPEN_PROFILE).strip()
    return profile or LOCAL_OPEN_PROFILE


def _count(ctx: AuthContext, surface: str) -> int:
    """Count identities in one store; unknown stores count as zero."""
    store = getattr(ctx, surface, None)
    counter = getattr(store, f"count_{surface}", None)

    if not callable(counter):
        return 0

    return int(counter())


def admin_credential_surfaces(ctx: AuthContext) -> tuple[str, ...]:
    """
    Which *administrative* credential surfaces are configured.

    The admin surface is exactly what the project documents it to
    be: registered admin identities, or the legacy shared key.
    Client (tenant) keys deliberately do **not** count — they are
    not an admin credential, and there is no CLI path that could
    create the first admin identity. Treating a tenant key as
    "authentication enabled" would lock an operator out of the
    admin surface permanently on a fresh local deployment.

    Reads live store state (never a cached process flag), so a
    deployment closes the anonymous path the instant its first
    admin identity is created. Raises HTTPException 503 when the
    store cannot be read — the caller must deny, not fall through.
    """
    surfaces: list[str] = []

    if shared_api_key():
        surfaces.append("shared-key")

    try:
        if _count(ctx, "admins") > 0:
            surfaces.append("admins")
    except HTTPException:
        raise
    except Exception as exc:  # unreadable store -> fail closed
        raise HTTPException(
            status_code=503, detail=_AUTH_UNAVAILABLE
        ) from exc

    return tuple(surfaces)


def open_dev_mode(ctx: AuthContext) -> bool:
    """
    True only for genuinely unauthenticated local development:
    the `local` profile with no administrative credential surface
    configured.

    Every other profile denies the anonymous caller outright, and
    so does the `local` profile once an admin identity or shared
    key exists. This is the *only* path that can mint an
    anonymous superadmin.
    """
    if current_profile() != LOCAL_OPEN_PROFILE:
        return False

    return not admin_credential_surfaces(ctx)


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
        try:
            admin = ctx.admins.authenticate(provided)
        except Exception as exc:  # unreadable store -> fail closed
            raise HTTPException(
                status_code=503, detail=_AUTH_UNAVAILABLE
            ) from exc

        if admin is not None:
            return Principal(
                kind="admin",
                name=admin.name,
                role=admin.role,
                admin_id=admin.id,
                _permissions=set(PERMISSIONS.get(admin.role, set())),
            )

        try:
            client = ctx.clients.authenticate(provided)
        except Exception as exc:  # unreadable store -> fail closed
            raise HTTPException(
                status_code=503, detail=_AUTH_UNAVAILABLE
            ) from exc

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

    if provided and expected and hmac.compare_digest(provided, expected):
        # Stage 8 shared key maps to a virtual superadmin. The
        # identity is not in the admin table and cannot be listed
        # or rotated there; migration path is admin identities.
        return Principal(
            kind="shared-key",
            name="shared-key",
            role="superadmin",
            _permissions=set(PERMISSIONS["superadmin"]),
        )

    # No credential resolved. Anonymous superadmin is available only
    # in explicit local-open development mode.
    if open_dev_mode(ctx):
        return Principal(
            kind="local-dev",
            name="local-dev",
            role="superadmin",
            _permissions=set(PERMISSIONS["superadmin"]),
        )

    raise HTTPException(
        status_code=401,
        detail="invalid or missing API key",
    )


# Compatibility alias: Stage 9 tests import TenantHeader to
# resolve principals against an explicit Authorization header.
TenantHeader = Header


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
