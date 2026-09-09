"""
Stage 8.1/8.7/8.8/8.10 + Stage 10.3/10.4/10.7: hardened public API.

Canonical behavior:

    POST /api/v1/missions
      -> validate -> govern -> persist -> enqueue -> return now

    {"id": "...", "status": "QUEUED"}

Long missions never hold the HTTP connection open; clients poll
GET /api/v1/missions/{id}.

Authorization (Stage 10.3):

- every request resolves to a Principal (admin identity, client
  identity, legacy shared key, or local-dev) — see app/api/auth.py
- endpoint guards check RBAC permissions from the matrix in
  app/api/rbac.py
- client principals are isolation-scoped: mission reads, evidence,
  and events return only missions attributed to that client

Governance (Stage 10.4):

- per-client token-bucket rate limits (multi-process safe) with
  429 + Retry-After / X-RateLimit-* headers
- payload limits (goal length, metadata size, body size) enforced
  before any expensive processing
- every rate-limit or governance rejection is audited
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from app.api.auth import Principal, require, resolve_principal
from app.api.governance import (
    DEFAULT_GOVERNANCE,
    DEFAULT_RATE,
    RateLimitConfig,
    RateLimiter,
    check_payload_governance,
)
from app.config import load_config, profile_defaults
from app.core.models import (
    AdminCreate,
    AdminRoleUpdate,
    ClientCreate,
    ClientPriorityUpdate,
    ClientQuotaUpdate,
    Mission,
    MissionCreate,
    MissionStatus,
)
from app.runtime.repo_leases import RepoLeaseManager
from app.storage.sqlite_store import MissionStore, DuplicateMission
from app.tenants.admins import AdminStore
from app.tenants.audit import AuditStore
from app.tenants.clients import ClientStore
from app.workers.registry import registry
from app.learning.engine import store as learning_store


store = MissionStore()
clients = ClientStore()
admins = AdminStore()
audit = AuditStore()

_leases: RepoLeaseManager | None = None


def leases() -> RepoLeaseManager:
    """Process-wide repo lease manager on the mission-store DB."""
    global _leases
    if _leases is None:
        _leases = RepoLeaseManager(store.path)
    return _leases


# ---------------------------------------------------------
# Rate limiting (Stage 10.4)
#
# Profile-aware: the local profile ships with rate limiting off
# (10.7 defaults); every other profile enforces. YODAW_RATE_LIMIT_RPM
# overrides explicitly (0 disables, N sets the steady rate).
# ---------------------------------------------------------


def _rate_limit_enabled() -> bool:
    env = os.environ.get("YODAW_RATE_LIMIT_RPM", "").strip()

    if env:
        return env != "0"

    profile = os.environ.get("YODAW_PROFILE", "local").strip()

    return profile != "local"


_rate_backend = MissionStore(store.path)
rate_limiter = RateLimiter(
    _rate_backend,
    enabled=_rate_limit_enabled(),
    config=RateLimitConfig(
        requests_per_minute=int(
            os.environ.get("YODAW_RATE_LIMIT_RPM", "60") or 60
        ) or 60,
        missions_per_minute=int(
            os.environ.get("YODAW_MISSIONS_PER_MINUTE", "10") or 10
        ) or 10,
    ),
)


def _governance_config_for(principal: Principal):
    """Per-client governance overrides are future work; today the
    global defaults apply to every caller uniformly."""
    return DEFAULT_GOVERNANCE


def _enforce_rate_limit(principal: Principal) -> None:
    """
    Token-bucket admission for every governed request.

    429 with Retry-After and X-RateLimit-* headers; the rejection
    is audited. Local-dev and shared-key principals rate-limit
    under their own bucket keys too.
    """
    bucket_key = principal.client_id or principal.admin_id or (
        f"{principal.kind}:{principal.name}"
    )

    allowed, retry_after = rate_limiter.check(bucket_key)

    if not allowed:
        audit.append(
            client_id=principal.client_id,
            actor=principal.name,
            action="request.rate_limited",
            data={
                "bucket": bucket_key,
                "retry_after_seconds": round(retry_after, 2),
                "via": principal.kind,
            },
        )

        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={
                "Retry-After": str(max(1, int(retry_after) + 1)),
                "X-RateLimit-Limit": str(DEFAULT_RATE.requests_per_minute),
                "X-RateLimit-Remaining": "0",
            },
        )


_coordinator = None
_coordinator_lock = None


def get_coordinator():
    """
    Lazy, idempotent coordinator accessor.

    Starts the embedded coordinator on first mission creation (or
    on demand in tests). Multi-process deployments disable
    embedding via YODAW_EMBED_COORDINATOR=0.
    """
    global _coordinator, _coordinator_lock

    if os.environ.get("YODAW_EMBED_COORDINATOR", "1") != "1":
        return None

    if _coordinator is not None:
        return _coordinator

    if _coordinator_lock is None:
        _coordinator_lock = threading.Lock()

    with _coordinator_lock:
        if _coordinator is None:
            from app.runtime.coordinator import Coordinator

            def client_limits() -> dict:
                return {
                    c["id"]: c["max_concurrent_missions"]
                    for c in clients.list_clients()
                    if c["max_concurrent_missions"] is not None
                }

            coordinator = Coordinator(
                store=store,
                client_limits_provider=client_limits,
            )
            coordinator.start()
            _coordinator = coordinator

    return _coordinator


@asynccontextmanager
async def lifespan(_app):
    """
    Startup validates the deployment profile and fails fast on
    unsafe production combinations (Stage 10.7).
    """
    try:
        load_config()
    except Exception as exc:
        # Surface the ConfigError message in the failure.
        raise RuntimeError(f"configuration rejected: {exc}") from exc

    get_coordinator()
    yield

    coordinator = _coordinator

    if coordinator is not None:
        coordinator.stop(drain=True, timeout=20)


app = FastAPI(
    title="YODAW Code Core",
    version="0.3.0",
    lifespan=lifespan,
)


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]

    return ""


def auth_mode() -> str:
    """Open local-dev, shared-key, or identity-based RBAC mode."""
    if os.environ.get("YODAW_HAS_IDENTITIES") == "1":
        return "rbac"
    return "key" if os.environ.get("YODAW_API_KEY") else "local-dev"


def _identity(authorization: str | None) -> dict:
    """
    Stage 9 compatibility shim: resolve the caller to the flat
    identity dict legacy endpoints/tests consume.
    """
    principal = resolve_principal(authorization)

    return {
        "client_id": principal.client_id,
        "name": principal.name,
        "priority": principal.priority,
        "max_concurrent_missions": principal.max_concurrent_missions,
        "via": principal.kind,
        "role": principal.role,
        "_principal": principal,
    }


def _guard(authorization: str | None) -> dict:
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)
    return _identity(authorization)


# ---------------------------------------------------------
# Health (open) + runtime status
# ---------------------------------------------------------


@app.get("/api/v1/health")
def health():
    try:
        cfg = load_config()
        config_ok = True
        config_error = None
    except Exception as exc:
        cfg = None
        config_ok = False
        config_error = str(exc)

    return {
        "service": "YODAW",
        "status": "READY",
        "auth": auth_mode(),
        "workers": registry.status(),
        "config_ok": config_ok,
        "config_error": config_error,
        "profile": cfg.profile if cfg else None,
    }


@app.get("/api/v1/workers")
def workers(authorization: str | None = Header(default=None)):
    _guard(authorization)
    return registry.status()


@app.get("/api/v1/runtime/status")
def runtime_status(
    authorization: str | None = Header(default=None),
):
    """
    Safe non-secret runtime/config state (Stage 10.7).

    Requires an authenticated caller; never includes key material,
    DSNs, or secrets — only booleans, counts, and profile names.
    """
    principal = resolve_principal(authorization)

    _enforce_rate_limit(principal)

    coordinator = _coordinator

    try:
        cfg = load_config()
        profile_state = {
            "profile": cfg.profile,
            "backend": cfg.backend,
            "auth_mode": cfg.auth_mode,
            "require_auth": cfg.require_auth,
            "embed_coordinator": cfg.embed_coordinator,
            "warnings": cfg.warnings,
            "defaults": profile_defaults(cfg.profile),
        }
    except Exception as exc:
        profile_state = {
            "profile": None,
            "config_error": str(exc),
        }

    return {
        "auth": auth_mode(),
        "missions": store.status_counts(),
        "workers": registry.status(),
        "outbox": store.outbox_stats(),
        "clients": clients.count_clients(),
        "admins": admins.count_admins(),
        "coordinator": (
            coordinator.stats()
            if coordinator is not None
            else {"embedded": False}
        ),
        "configuration": profile_state,
    }


# ---------------------------------------------------------
# Missions (client surface + isolation)
# ---------------------------------------------------------


def _can_read_mission(principal: Principal, mission: Mission) -> bool:
    """
    Client isolation: a client principal may only read missions it
    submitted. Admins, operators, auditors, shared-key, and
    local-dev callers read everything.
    """
    if principal.kind != "client":
        return True

    return mission.client_id == principal.client_id


def _load_mission_for(principal: Principal, mission_id: str) -> Mission:
    mission = store.get(mission_id)

    if not mission:
        raise HTTPException(404, "Mission not found")

    if not _can_read_mission(principal, mission):
        # 404, not 403: never reveal the existence of another
        # client's mission.
        raise HTTPException(404, "Mission not found")

    return mission


@app.post("/api/v1/missions")
def create_mission(
    request: MissionCreate,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("missions.create"):
        raise HTTPException(
            403, detail="missing permission: missions.create"
        )

    _enforce_rate_limit(principal)

    # Governance before any expensive processing (Stage 10.4).
    ok, reason = check_payload_governance(
        goal=request.goal,
        metadata=request.metadata,
        config=_governance_config_for(principal),
    )

    if not ok:
        audit.append(
            client_id=principal.client_id,
            actor=principal.name,
            action="mission.rejected",
            data={
                "reason": "payload_governance",
                "detail": reason,
                "via": principal.kind,
            },
        )

        raise HTTPException(413, detail=reason)

    # Mission creation rate (separate from the general bucket).
    bucket_key = principal.client_id or principal.admin_id or (
        f"{principal.kind}:{principal.name}"
    )

    allowed, retry_after = rate_limiter.check(
        f"missions:{bucket_key}",
        rpm=DEFAULT_RATE.missions_per_minute,
        burst=DEFAULT_RATE.missions_per_minute,
    )

    if not allowed:
        audit.append(
            client_id=principal.client_id,
            actor=principal.name,
            action="mission.rejected",
            data={
                "reason": "rate_limited",
                "scope": "mission_creation",
                "retry_after_seconds": round(retry_after, 2),
                "via": principal.kind,
            },
        )

        raise HTTPException(
            status_code=429,
            detail="mission creation rate limit exceeded",
            headers={
                "Retry-After": str(max(1, int(retry_after) + 1)),
            },
        )

    # Validate capability before persisting.
    worker = registry.find(request.capability)

    if worker is None:
        mission = Mission(
            goal=request.goal,
            capability=request.capability,
            metadata=request.metadata,
            client_id=principal.client_id,
            priority=principal.priority,
        )
        mission.status = MissionStatus.blocked
        mission.result = {
            "error": f"No worker for capability: {request.capability}"
        }
        mission.finished_at = mission.updated_at
        store.save(mission)

        audit.append(
            client_id=principal.client_id,
            actor=principal.name,
            action="mission.rejected",
            mission_id=mission.id,
            data={
                "reason": "no_worker",
                "capability": request.capability,
                "client": principal.name,
            },
        )

        return {
            "id": mission.id,
            "status": mission.status.value,
            "detail": "no worker for capability; mission blocked",
        }

    # Stage 9: per-client concurrency quota (fast admission check).
    limit = principal.max_concurrent_missions

    if (
        principal.client_id is not None
        and limit is not None
        and store.executing_count_for_client(principal.client_id)
        >= limit
    ):
        audit.append(
            client_id=principal.client_id,
            actor=principal.name,
            action="mission.rejected",
            data={
                "reason": "quota_exceeded",
                "limit": limit,
                "client": principal.name,
            },
        )

        raise HTTPException(
            status_code=429,
            detail=(
                f"client concurrency limit reached ({limit} active "
                "missions)"
            ),
        )

    mission = Mission(
        goal=request.goal,
        capability=request.capability,
        metadata=request.metadata,
        client_id=principal.client_id,
        priority=principal.priority,
    )

    try:
        store.enqueue(mission)
    except DuplicateMission as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-YODAW-Active-Mission": exc.mission_id or ""},
        )

    audit.append(
        client_id=principal.client_id,
        actor=principal.name,
        action="mission.created",
        mission_id=mission.id,
        data={
            "goal": mission.goal,
            "capability": mission.capability,
            "priority": mission.priority,
            "client": principal.name,
        },
    )

    coordinator = get_coordinator()

    if coordinator is not None:
        coordinator.wake()

    return {
        "id": mission.id,
        "status": mission.status.value,
    }


@app.get("/api/v1/missions")
def list_missions(
    limit: int = 100,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    if principal.kind == "client":
        # Isolation-scoped listing.
        missions = [
            m
            for m in store.list()
            if m.client_id == principal.client_id
        ]
    else:
        missions = store.list()

    start = max(0, int(offset))
    end = start + max(1, min(int(limit), 500))

    return missions[start:end]


@app.get("/api/v1/missions/{mission_id}")
def get_mission(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    mission = _load_mission_for(principal, mission_id)

    return mission


@app.get("/api/v1/missions/{mission_id}/evidence")
def get_evidence(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    mission = _load_mission_for(principal, mission_id)

    return {
        "mission_id": mission.id,
        "evidence": mission.evidence,
    }


@app.get("/api/v1/missions/{mission_id}/events")
def get_events(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    mission = _load_mission_for(principal, mission_id)

    return {
        "mission_id": mission.id,
        "events": store.events(mission.id),
    }


@app.post("/api/v1/missions/{mission_id}/cancel")
def cancel_mission(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("missions.cancel"):
        raise HTTPException(
            403, detail="missing permission: missions.cancel"
        )

    _enforce_rate_limit(principal)

    if principal.kind == "client":
        # Clients cancel only their own missions.
        mission = store.get(mission_id)

        if not mission or mission.client_id != principal.client_id:
            raise HTTPException(404, "Mission not found")

    outcome = store.request_cancel(mission_id)

    if outcome == "unknown":
        raise HTTPException(404, "Mission not found")

    if outcome == "terminal":
        raise HTTPException(409, "Mission already finished")

    audit.append(
        client_id=principal.client_id,
        actor=principal.name,
        action="mission.cancel_requested",
        mission_id=mission_id,
        data={"outcome": outcome, "client": principal.name},
    )

    if outcome == "cancelled":
        return {"id": mission_id, "status": "CANCELLED"}

    return {
        "id": mission_id,
        "status": "CANCELLING",
        "detail": "cancellation requested; worker will stop at "
        "the next checkpoint",
    }


@app.get("/api/v1/learning")
def list_learning(authorization: str | None = Header(default=None)):
    _guard(authorization)

    return learning_store.list()


# ---------------------------------------------------------
# Stage 10.3: RBAC admin surface
# ---------------------------------------------------------


def _audit_admin_action(principal: Principal, action: str, data: dict):
    audit.append(
        client_id=principal.client_id,
        actor=principal.name,
        action=action,
        data={"actor": principal.name, **data},
    )


@app.post("/api/v1/admins")
def create_admin(
    request: AdminCreate,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    # Exact-role gate: only superadmins manage admins.
    if principal.role != "superadmin":
        raise HTTPException(
            403,
            detail="admin management requires the superadmin role",
        )

    try:
        created = admins.create_admin(
            name=request.name, role=request.role
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # The plaintext key is returned exactly once and never
    # persisted or audited.
    _audit_admin_action(
        principal,
        "admins.created",
        {"name": created["name"], "role": created["role"]},
    )

    if auth_mode() != "rbac":
        os.environ["YODAW_HAS_IDENTITIES"] = "1"

    return created


@app.get("/api/v1/admins")
def list_admins(
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if principal.role != "superadmin":
        raise HTTPException(
            403,
            detail="admin management requires the superadmin role",
        )

    # Never includes key hashes or plaintext keys.
    return admins.list_admins()


@app.post("/api/v1/admins/{name}/rotate")
def rotate_admin_key(
    name: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if principal.role != "superadmin":
        raise HTTPException(
            403,
            detail="admin management requires the superadmin role",
        )

    rotated = admins.rotate_key(name)

    if rotated is None:
        raise HTTPException(404, "Admin not found or disabled")

    _audit_admin_action(
        principal, "admins.key_rotated", {"name": name}
    )

    return rotated


@app.post("/api/v1/admins/{name}/role")
def set_admin_role(
    name: str,
    request: AdminRoleUpdate,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if principal.role != "superadmin":
        raise HTTPException(
            403,
            detail="admin management requires the superadmin role",
        )

    try:
        changed = admins.set_role(name, request.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    if not changed:
        raise HTTPException(404, "Admin not found")

    _audit_admin_action(
        principal,
        "admins.role_set",
        {"name": name, "role": request.role},
    )

    return {"name": name, "role": request.role}


@app.post("/api/v1/admins/{name}/disable")
def disable_admin(
    name: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if principal.role != "superadmin":
        raise HTTPException(
            403,
            detail="admin management requires the superadmin role",
        )

    if not admins.set_disabled(name, True):
        raise HTTPException(404, "Admin not found")

    _audit_admin_action(principal, "admins.disabled", {"name": name})

    return {"name": name, "disabled": True}


@app.post("/api/v1/admins/{name}/enable")
def enable_admin(
    name: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if principal.role != "superadmin":
        raise HTTPException(
            403,
            detail="admin management requires the superadmin role",
        )

    if not admins.set_disabled(name, False):
        raise HTTPException(404, "Admin not found")

    _audit_admin_action(principal, "admins.enabled", {"name": name})

    return {"name": name, "disabled": False}


# ---------------------------------------------------------
# Stage 9 client management (now RBAC-guarded)
# ---------------------------------------------------------


@app.post("/api/v1/clients")
def create_client(
    request: ClientCreate,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    if not principal.can("clients.manage"):
        raise HTTPException(
            403, detail="missing permission: clients.manage"
        )

    try:
        created = clients.create_client(
            name=request.name,
            priority=request.priority,
            max_concurrent_missions=(
                request.max_concurrent_missions
            ),
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc))

    _audit_admin_action(
        principal,
        "clients.created",
        {
            "name": created["name"],
            "priority": created["priority"],
            "max_concurrent_missions": (
                created["max_concurrent_missions"]
            ),
        },
    )

    return created


@app.get("/api/v1/clients")
def list_clients(
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("clients.manage"):
        raise HTTPException(
            403, detail="missing permission: clients.manage"
        )

    # Admin listing never includes key hashes or plaintext keys.
    return clients.list_clients()


@app.post("/api/v1/clients/{name}/disable")
def disable_client(
    name: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("clients.manage"):
        raise HTTPException(
            403, detail="missing permission: clients.manage"
        )

    if not clients.set_disabled(name, True):
        raise HTTPException(404, "Client not found")

    _audit_admin_action(principal, "clients.disabled", {"name": name})

    return {"name": name, "disabled": True}


@app.post("/api/v1/clients/{name}/enable")
def enable_client(
    name: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("clients.manage"):
        raise HTTPException(
            403, detail="missing permission: clients.manage"
        )

    if not clients.set_disabled(name, False):
        raise HTTPException(404, "Client not found")

    _audit_admin_action(principal, "clients.enabled", {"name": name})

    return {"name": name, "disabled": False}


@app.post("/api/v1/clients/{name}/priority")
def set_client_priority(
    name: str,
    request: ClientPriorityUpdate,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("clients.manage"):
        raise HTTPException(
            403, detail="missing permission: clients.manage"
        )

    if not clients.set_priority(name, request.priority):
        raise HTTPException(404, "Client not found")

    _audit_admin_action(
        principal,
        "clients.priority_set",
        {"name": name, "priority": request.priority},
    )

    return {"name": name, "priority": request.priority}


@app.post("/api/v1/clients/{name}/quota")
def set_client_quota(
    name: str,
    request: ClientQuotaUpdate,
    authorization: str | None = Header(default=None),
):
    """Stage 10.3: quota management under clients.manage."""
    principal = resolve_principal(authorization)

    if not principal.can("clients.manage"):
        raise HTTPException(
            403, detail="missing permission: clients.manage"
        )

    if not clients.set_quota(name, request.max_concurrent_missions):
        raise HTTPException(404, "Client not found")

    _audit_admin_action(
        principal,
        "clients.quota_set",
        {
            "name": name,
            "max_concurrent_missions": (
                request.max_concurrent_missions
            ),
        },
    )

    return {
        "name": name,
        "max_concurrent_missions": (
            request.max_concurrent_missions
        ),
    }


# ---------------------------------------------------------
# Stage 10.5: audit + integrity endpoints
# ---------------------------------------------------------


@app.get("/api/v1/audit")
def query_audit(
    client_id: str | None = None,
    mission_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
    offset: int = 0,
    authorization: str | None = Header(default=None),
):
    """Compliance view of the tamper-evident audit trail."""
    principal = resolve_principal(authorization)

    if not principal.can("audit.read"):
        raise HTTPException(403, "missing permission: audit.read")

    _enforce_rate_limit(principal)

    if principal.kind == "client":
        # Clients see only their own audit rows, and only via the
        # dedicated endpoint below; the global trail is admin-only.
        raise HTTPException(
            403,
            detail="the global audit trail requires auditor access",
        )

    return {
        "events": audit.query(
            client_id=client_id,
            mission_id=mission_id,
            action=action,
            limit=limit,
            offset=offset,
        ),
    }


@app.get("/api/v1/audit/verify")
def verify_audit(
    authorization: str | None = Header(default=None),
):
    """Tamper-evidence check over the full chain."""
    principal = resolve_principal(authorization)

    if not principal.can("audit.read"):
        raise HTTPException(403, "missing permission: audit.read")

    _enforce_rate_limit(principal)

    return audit.verify()


@app.post("/api/v1/audit/prune")
def prune_audit(
    request: AuditPruneRequest,
    authorization: str | None = Header(default=None),
):
    """Retention operation: prune (optionally archive) old events."""
    principal = resolve_principal(authorization)

    if principal.role != "superadmin":
        raise HTTPException(
            403,
            detail="audit retention requires the superadmin role",
        )

    _enforce_rate_limit(principal)

    try:
        result = audit.prune(
            keep_days=request.keep_days,
            archive_path=request.archive_path,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    _audit_admin_action(
        principal,
        "audit.pruned",
        {
            "keep_days": request.keep_days,
            "pruned": result["pruned"],
            "archived": result["archived"],
            "head_seq": result["head_seq"],
        },
    )

    return result


# ---------------------------------------------------------
# Stage 10.6: outbox operations surface
# ---------------------------------------------------------


@app.get("/api/v1/outbox")
def list_outbox(
    limit: int = 100,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("outbox.manage"):
        raise HTTPException(403, "missing permission: outbox.manage")

    _enforce_rate_limit(principal)

    stats = store.outbox_stats()
    pending = store.outbox_pending(limit=max(1, min(int(limit), 500)))

    return {"stats": stats, "pending": pending}


@app.get("/api/v1/outbox/dead")
def list_dead_outbox(
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("outbox.manage"):
        raise HTTPException(403, "missing permission: outbox.manage")

    _enforce_rate_limit(principal)

    return {"dead": store.outbox_list_dead()}


@app.post("/api/v1/outbox/{outbox_id}/requeue")
def requeue_outbox(
    outbox_id: int,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("outbox.manage"):
        raise HTTPException(403, "missing permission: outbox.manage")

    _enforce_rate_limit(principal)

    count = store.outbox_requeue_dead(outbox_id)

    if count == 0:
        raise HTTPException(404, "No dead-lettered message with this id")

    _audit_admin_action(
        principal,
        "outbox.requeued",
        {"outbox_id": outbox_id},
    )

    return {"requeued": count}


@app.post("/api/v1/outbox/{outbox_id}/dead-letter")
def dead_letter_outbox(
    outbox_id: int,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)

    if not principal.can("outbox.manage"):
        raise HTTPException(403, "missing permission: outbox.manage")

    _enforce_rate_limit(principal)

    message = store.outbox_message(outbox_id)

    if message is None or message["delivered_at"] is not None:
        raise HTTPException(404, "No pending message with this id")

    store.outbox_dead_letter(outbox_id)

    _audit_admin_action(
        principal,
        "outbox.dead_lettered",
        {"outbox_id": outbox_id, "kind": message["kind"]},
    )

    return {"dead_lettered": outbox_id}
