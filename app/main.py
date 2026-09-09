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

from app.core.models import Mission, MissionCreate, MissionStatus
from app.storage.sqlite_store import MissionStore, DuplicateMission
from app.workers.registry import registry
from app.learning.engine import store as learning_store


store = MissionStore()

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

            coordinator = Coordinator(store=store)
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
    return "key" if api_key() else "local-dev"


def _authorize(authorization: str | None) -> None:
    expected = api_key()

    if not expected:
        # Local dev mode: no key configured, access stays open.
        return

    provided = ""

    if authorization and authorization.startswith("Bearer "):
        provided = authorization[len("Bearer "):]

    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="invalid or missing API key",
        )


def _guard(authorization: str | None) -> None:
    _authorize(authorization)


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
    """Optional Stage 8 observability endpoint."""
    _guard(authorization)

    return {
        "auth": auth_mode(),
        "missions": store.status_counts(),
        "workers": registry.status(),
    }


@app.post("/api/v1/missions")
def create_mission(
    request: MissionCreate,
    authorization: str | None = Header(default=None),
):
    _guard(authorization)

    # Validate capability before persisting so unknown
    # capabilities fail fast with 422 semantics.
    worker = registry.find(request.capability)

    if worker is None:
        mission = Mission(
            goal=request.goal,
            capability=request.capability,
            metadata=request.metadata,
        )
        mission.status = MissionStatus.blocked
        mission.result = {
            "error": f"No worker for capability: {request.capability}"
        }
        mission.finished_at = mission.updated_at
        store.save(mission)

        return {
            "id": mission.id,
            "status": mission.status.value,
            "detail": "no worker for capability; mission blocked",
        }

    mission = Mission(
        goal=request.goal,
        capability=request.capability,
        metadata=request.metadata,
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
    _guard(authorization)

    outcome = store.request_cancel(mission_id)

    if outcome == "unknown":
        raise HTTPException(404, "Mission not found")

    if outcome == "terminal":
        raise HTTPException(409, "Mission already finished")

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
