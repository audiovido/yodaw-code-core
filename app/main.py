"""
Stage 8.1/8.7/8.8/8.10: hardened public API.

Canonical behavior:

    POST /api/v1/missions
      -> validate -> persist -> enqueue -> return immediately

    {"id": "...", "status": "QUEUED"}

Long missions no longer hold the HTTP connection open; clients
poll GET /api/v1/missions/{id}.

Authentication (Stage 8.7):
- when YODAW_API_KEY is set, all /api/v1 routes except /health
  require Authorization: Bearer <key>
- constant-time comparison; the key is never logged
- without a key the API runs in local dev mode (open access),
  the explicitly supported local configuration

Stage 7 compatibility:
- GET /api/v1/health, /api/v1/workers, /api/v1/learning remain
- mission payloads keep the same shape; QUEUED missions expose
  the queue metadata instead of a worker result
"""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from app.core.models import (
    ClientCreate,
    ClientPriorityUpdate,
    Mission,
    MissionCreate,
    MissionStatus,
)
from app.storage.sqlite_store import MissionStore, DuplicateMission
from app.tenants.clients import ClientStore
from app.tenants.audit import AuditStore
from app.workers.registry import registry
from app.learning.engine import store as learning_store


store = MissionStore()
clients = ClientStore()
audit = AuditStore()

_coordinator = None
_coordinator_lock = None


def get_coordinator():
    """
    Lazy, idempotent coordinator accessor.

    Starts the embedded coordinator on first mission creation (or
    on demand in tests). This keeps module import cheap and lets
    multi-process deployments disable embedding entirely via
    YODAW_EMBED_COORDINATOR=0.
    """
    global _coordinator, _coordinator_lock

    if os.environ.get("YODAW_EMBED_COORDINATOR", "1") != "1":
        return None

    if _coordinator is not None:
        return _coordinator

    if _coordinator_lock is None:
        import threading

        _coordinator_lock = threading.Lock()

    with _coordinator_lock:
        if _coordinator is None:
            from app.runtime.coordinator import Coordinator

            def client_limits() -> dict:
                """
                Stage 9: per-client concurrency limits for claim-time
                enforcement, resolved fresh on every claim pass so
                admin changes apply immediately.
                """
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
    Production path: embed the coordinator in the API process by
    default (YODAW_EMBED_COORDINATOR=1). For multi-process
    deployments, set YODAW_EMBED_COORDINATOR=0 and run standalone
    coordinators against the same database instead.
    """
    get_coordinator()
    yield

    coordinator = _coordinator

    if coordinator is not None:
        coordinator.stop(drain=True, timeout=20)


app = FastAPI(
    title="YODAW Code Core",
    version="0.2.0",
    lifespan=lifespan,
)


def api_key() -> str:
    return os.environ.get("YODAW_API_KEY", "")


def auth_mode() -> str:
    """Shared-key auth configured, or open local-dev mode."""
    return "key" if api_key() else "local-dev"


def _bearer(authorization: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]

    return ""


def _identity(authorization: str | None) -> dict:
    """
    Stage 9 authentication.

    Resolution order for the bearer key:
    1. a registered client identity (hashed lookup in the
       api_clients table) - carries priority and quotas
    2. the shared environment key (YODAW_API_KEY), preserving the
       Stage 8 single-tenant contract - no client identity

    If a shared key is configured and neither matches, the request
    is rejected. With no shared key configured the API runs in
    local-dev mode (open access), the explicitly supported local
    configuration; registered client keys still authenticate.
    """
    provided = _bearer(authorization)

    if provided:
        record = clients.authenticate(provided)

        if record is not None:
            return {
                "client_id": record.id,
                "name": record.name,
                "priority": record.priority,
                "max_concurrent_missions": (
                    record.max_concurrent_missions
                ),
                "via": "client-key",
            }

    expected = api_key()

    if not expected:
        # Local dev mode: no shared key configured, access stays
        # open without a client identity.
        return {
            "client_id": None,
            "name": "local-dev",
            "priority": 5,
            "max_concurrent_missions": None,
            "via": "local-dev",
        }

    if provided and hmac.compare_digest(provided, expected):
        # Stage 8 shared-key callers keep full access but carry no
        # client identity (no priority class, no quota).
        return {
            "client_id": None,
            "name": "shared-key",
            "priority": 5,
            "max_concurrent_missions": None,
            "via": "shared-key",
        }

    raise HTTPException(
        status_code=401,
        detail="invalid or missing API key",
    )


def _guard(authorization: str | None) -> dict:
    return _identity(authorization)


def _require_shared_key(authorization: str | None) -> dict:
    """
    Admin gate for client management.

    Client identities are never admins: administration is reserved
    for the shared environment key, or local-dev when no key is
    configured (single-operator local deployments).
    """
    identity = _identity(authorization)

    if identity["via"] not in ("shared-key", "local-dev"):
        raise HTTPException(
            status_code=403,
            detail="client management requires the shared key",
        )

    return identity


@app.get("/api/v1/health")
def health():
    return {
        "service": "YODAW",
        "status": "READY",
        "auth": auth_mode(),
        "workers": registry.status(),
    }


@app.get("/api/v1/workers")
def workers(authorization: str | None = Header(default=None)):
    _guard(authorization)

    return registry.status()


@app.get("/api/v1/runtime/status")
def runtime_status(authorization: str | None = Header(default=None)):
    """Runtime observability: queue, workers, outbox, tenancy."""
    _guard(authorization)

    coordinator = _coordinator

    return {
        "auth": auth_mode(),
        "missions": store.status_counts(),
        "workers": registry.status(),
        "outbox": store.outbox_stats(),
        "clients": clients.count_clients(),
        "coordinator": (
            coordinator.stats()
            if coordinator is not None
            else {"embedded": False}
        ),
    }


@app.post("/api/v1/missions")
def create_mission(
    request: MissionCreate,
    authorization: str | None = Header(default=None),
):
    identity = _guard(authorization)

    # Validate capability before persisting so unknown
    # capabilities fail fast with 422 semantics.
    worker = registry.find(request.capability)

    if worker is None:
        mission = Mission(
            goal=request.goal,
            capability=request.capability,
            metadata=request.metadata,
            client_id=identity["client_id"],
            priority=identity["priority"],
        )
        mission.status = MissionStatus.blocked
        mission.result = {
            "error": f"No worker for capability: {request.capability}"
        }
        mission.finished_at = mission.updated_at
        store.save(mission)

        audit.append(
            client_id=identity["client_id"],
            action="mission.rejected",
            mission_id=mission.id,
            data={
                "reason": "no_worker",
                "capability": request.capability,
                "client": identity["name"],
            },
        )

        return {
            "id": mission.id,
            "status": mission.status.value,
            "detail": "no worker for capability; mission blocked",
        }

    # Stage 9: per-client concurrency quota (admission control).
    # The authoritative enforcement is race-free inside claim_next;
    # this check fails fast at submission time.
    limit = identity["max_concurrent_missions"]

    if (
        identity["client_id"] is not None
        and limit is not None
        and store.executing_count_for_client(identity["client_id"])
        >= limit
    ):
        audit.append(
            client_id=identity["client_id"],
            action="mission.rejected",
            data={
                "reason": "quota_exceeded",
                "limit": limit,
                "client": identity["name"],
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
        client_id=identity["client_id"],
        priority=identity["priority"],
    )

    # enqueue persists the mission payload and runtime columns in
    # one transaction; no separate save is needed here.
    try:
        store.enqueue(mission)
    except DuplicateMission as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-YODAW-Active-Mission": exc.mission_id or ""},
        )

    audit.append(
        client_id=identity["client_id"],
        action="mission.created",
        mission_id=mission.id,
        data={
            "goal": mission.goal,
            "capability": mission.capability,
            "priority": mission.priority,
            "client": identity["name"],
        },
    )

    # Execution is independent of this HTTP connection.
    coordinator = get_coordinator()

    if coordinator is not None:
        coordinator.wake()

    return {
        "id": mission.id,
        "status": mission.status.value,
    }


@app.get("/api/v1/missions")
def list_missions(authorization: str | None = Header(default=None)):
    _guard(authorization)

    return store.list()


@app.get("/api/v1/missions/{mission_id}")
def get_mission(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    _guard(authorization)

    mission = store.get(mission_id)

    if not mission:
        raise HTTPException(404, "Mission not found")

    return mission


@app.get("/api/v1/missions/{mission_id}/evidence")
def get_evidence(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    _guard(authorization)

    mission = store.get(mission_id)

    if not mission:
        raise HTTPException(404, "Mission not found")

    return {
        "mission_id": mission.id,
        "evidence": mission.evidence,
    }


@app.get("/api/v1/missions/{mission_id}/events")
def get_events(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    _guard(authorization)

    mission = store.get(mission_id)

    if not mission:
        raise HTTPException(404, "Mission not found")

    return {
        "mission_id": mission.id,
        "events": store.events(mission.id),
    }


@app.post("/api/v1/missions/{mission_id}/cancel")
def cancel_mission(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    identity = _guard(authorization)

    outcome = store.request_cancel(mission_id)

    if outcome == "unknown":
        raise HTTPException(404, "Mission not found")

    if outcome == "terminal":
        raise HTTPException(409, "Mission already finished")

    audit.append(
        client_id=identity["client_id"],
        action="mission.cancel_requested",
        mission_id=mission_id,
        data={"outcome": outcome, "client": identity["name"]},
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
# Stage 9.1/9.2: client identity administration + audit
# ---------------------------------------------------------


def _audit_client_action(
    identity: dict,
    action: str,
    data: dict,
):
    audit.append(
        client_id=identity["client_id"],
        action=action,
        data={"actor": identity["name"], **data},
    )


@app.post("/api/v1/clients")
def create_client(
    request: ClientCreate,
    authorization: str | None = Header(default=None),
):
    identity = _require_shared_key(authorization)

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

    # The plaintext key is returned exactly once and never
    # persisted or audited.
    _audit_client_action(
        identity,
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
    _require_shared_key(authorization)

    # Admin listing never includes key hashes or plaintext keys.
    return clients.list_clients()


@app.post("/api/v1/clients/{name}/disable")
def disable_client(
    name: str,
    authorization: str | None = Header(default=None),
):
    identity = _require_shared_key(authorization)

    if not clients.set_disabled(name, True):
        raise HTTPException(404, "Client not found")

    _audit_client_action(identity, "clients.disabled", {"name": name})

    return {"name": name, "disabled": True}


@app.post("/api/v1/clients/{name}/enable")
def enable_client(
    name: str,
    authorization: str | None = Header(default=None),
):
    identity = _require_shared_key(authorization)

    if not clients.set_disabled(name, False):
        raise HTTPException(404, "Client not found")

    _audit_client_action(identity, "clients.enabled", {"name": name})

    return {"name": name, "disabled": False}


@app.post("/api/v1/clients/{name}/priority")
def set_client_priority(
    name: str,
    request: ClientPriorityUpdate,
    authorization: str | None = Header(default=None),
):
    identity = _require_shared_key(authorization)

    if not clients.set_priority(name, request.priority):
        raise HTTPException(404, "Client not found")

    _audit_client_action(
        identity,
        "clients.priority_set",
        {"name": name, "priority": request.priority},
    )

    return {"name": name, "priority": request.priority}


@app.get("/api/v1/audit")
def query_audit(
    client_id: str | None = None,
    mission_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
    authorization: str | None = Header(default=None),
):
    """Compliance view of the append-only audit trail."""
    _require_shared_key(authorization)

    return {
        "events": audit.query(
            client_id=client_id,
            mission_id=mission_id,
            action=action,
            limit=limit,
        )
 }
