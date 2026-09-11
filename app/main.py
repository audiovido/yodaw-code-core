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
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ============================================================
# Standardized error envelope (RFC 7807 Problem Details)
# ============================================================

class ErrorEnvelope(BaseModel):
    """RFC 7807-compatible error envelope."""
    type: str = "about:blank"
    title: str
    status: int
    detail: Optional[str] = None
    instance: Optional[str] = None
    trace_id: Optional[str] = None
    errors: Optional[list[dict]] = None  # For validation errors

class ValidationErrorDetail(BaseModel):
    """Single validation error detail."""
    loc: list
    msg: str
    type: str

# ============================================================
# Versioning / API metadata
# ============================================================

API_VERSION = "v1"
API_TITLE = "YODAW Public API"

# ============================================================
# Standard exception handlers
# ============================================================

def _error_response(request: Request, exc: Exception, status: int, title: str, detail: str | None = None, headers: dict | None = None) -> JSONResponse:
    """Build a standardized error response."""
    from uuid import uuid4
    envelope = ErrorEnvelope(
        type=f"https://yodaw.ai/errors/{title.lower().replace(' ', '-')}",
        title=title,
        status=status,
        detail=detail or str(exc),
        instance=str(request.url.path),
        trace_id=f"trc_{uuid4().hex[:12]}",
    )
    return JSONResponse(
        status_code=status,
        content=envelope.model_dump(exclude_none=True),
        headers=headers,
    )

# ============================================================
# App & config
# ============================================================

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
    AuditPruneRequest,
)
from app.runtime.repo_leases import RepoLeaseManager
from app.storage.sqlite_store import MissionStore, DuplicateMission
from app.tenants.admins import AdminStore
from app.tenants.audit import AuditStore
from app.tenants.clients import ClientStore
from app.workers.registry import registry
from app.learning.engine import store as learning_store
from app.api.v1.missions import (
    ProductMissionSubmit,
    capabilities_view,
    product_view,
    retry_product_mission,
    submit_product_mission,
)
from app.mission.facade import select_skills


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
    enabled_provider=_rate_limit_enabled,
)


def _rate_limits_now() -> tuple[int, int, int]:
    """Live (rpm, burst, missions_per_minute) from the environment,
    so configuration changes apply without a process restart."""
    def _int(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)) or default)
        except ValueError:
            value = default
        return value if value > 0 else default

    return (
        _int("YODAW_RATE_LIMIT_RPM", 60),
        _int("YODAW_RATE_LIMIT_BURST", 10),
        _int("YODAW_MISSIONS_PER_MINUTE", 10),
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

    rpm, burst, _ = _rate_limits_now()

    allowed, retry_after = rate_limiter.check(
        bucket_key, rpm=rpm, burst=burst
    )

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
                "X-RateLimit-Limit": str(rpm),
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

# ---------------------------------------------------------
# Safe defaults: body-size guard + generic 500 mask
# ---------------------------------------------------------
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Enforce max request body size before payload reaches handlers."""
    def __init__(self, app, max_bytes: int = 256 * 1024):
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > self.max_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "request body too large"},
                    )
            except ValueError:
                pass
        return await call_next(request)

app.add_middleware(BodyLimitMiddleware, max_bytes=256 * 1024)


# API-A public contract handlers (RFC 7807 envelopes) subsume the
# generic 500 mask above: unhandled errors keep the envelope shape.
# ============================================================
# Exception handlers (standardized errors)
# ============================================================

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Convert HTTPException to RFC 7807 envelope (except 401/403 which keep minimal body)."""
    # 401 and 403 are intentionally minimal for security
    if exc.status_code in (401, 403):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail} if isinstance(exc.detail, str) else {"detail": "access denied"},
            headers=exc.headers,
        )
    # Preserve headers for 429 and other status codes
    return _error_response(request, exc, exc.status_code, exc.detail if isinstance(exc.detail, str) else "HTTP Error", headers=exc.headers)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Convert validation errors to RFC 7807 envelope with field details."""
    envelope = ErrorEnvelope(
        type="https://yodaw.ai/errors/validation-error",
        title="Validation Error",
        status=422,
        detail="Request body failed validation",
        instance=str(request.url.path),
        trace_id=f"trc_{__import__('uuid').uuid4().hex[:12]}",
        errors=[{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()],
    )
    return JSONResponse(status_code=422, content=envelope.model_dump(exclude_none=True))

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all: never expose stack traces, return 500 with envelope."""
    import logging
    logging.getLogger("yodaw.api").exception("Unhandled error")
    return _error_response(request, exc, 500, "Internal Server Error", "An unexpected error occurred")

def _bearer(authorization: str | None) -> str:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):]

    return ""


def auth_mode() -> str:
    """
    Open local-dev, shared-key, or identity-based RBAC mode.

    RBAC mode is derived from actual admin-identity state (never
    from a mutable process-global flag), so it is accurate in the
    serving process and cannot leak across test sessions or
    worker restarts.
    """
    if admins.count_admins() > 0:
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


@app.get("/api/v1/ready")
def readiness():
    """Readiness probe: returns 200 when coordinator is healthy and can accept work."""
    coordinator = _coordinator
    coordinator_ready = coordinator is None or coordinator.stats().get("running", False)
    try:
        cfg = load_config()
        config_ok = True
    except Exception:
        config_ok = False
    ready = coordinator_ready and config_ok
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "service": "YODAW",
            "ready": ready,
            "coordinator": "ok" if coordinator_ready else "unavailable",
            "config": "ok" if config_ok else "error",
            "profile": cfg.profile if config_ok else None,
        }
    )

@app.get("/api/v1/version")
def version():
    """API version and metadata."""
    return {
        "api_version": API_VERSION,
        "title": API_TITLE,
        "service_version": "0.3.0",
        "build": __import__("os").environ.get("YODAW_BUILD_SHA", "dev"),
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

# ============================================================
# Classification endpoint
# ============================================================

class ClassifyRequest(BaseModel):
    """Request for classification endpoint."""
    prompt: str
    context: dict = {}

@app.post("/api/v1/classify")
def classify_prompt(
    request: ClassifyRequest,
    authorization: str | None = Header(default=None),
):
    """
    Classify a prompt/task into capability and skill intent.

    Body: { "prompt": "task description", "context": {...} }
    Returns: { "capability": "...", "intent": "...", "skill": "...", "confidence": 0.0 }
    """
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    if not principal.can("missions.create"):
        raise HTTPException(403, detail="missing permission: missions.create")

    # Use the existing skill classifier
    try:
        from app.skills import TaskClassifier, SkillRegistry
        from app.skills.selection import SelectionContext, SkillSelector
        from app.skills.skills import (
            BugfixSkill, DocumentationSkill, FeatureSkill,
            RefactorSkill, ReviewSkill, TestSkill
        )

        registry_obj = SkillRegistry()
        for cls in (BugfixSkill, RefactorSkill, TestSkill, ReviewSkill, FeatureSkill, DocumentationSkill):
            try:
                registry_obj.register(cls())
            except Exception:
                continue

        classifier = TaskClassifier({s.id: s for s in registry_obj.get_all()})
        classification = classifier.classify(request.prompt, context={"language": request.context.get("language")})
        selection = SkillSelector(registry_obj).select(SelectionContext(
            task_description=request.prompt,
            classification=classification,
        ))

        # Map intent to capability
        capability_map = {
            "bugfix": "repo-code",
            "refactor": "repo-code",
            "test": "repo-code",
            "review": "repo-code",
            "feature": "repo-code",
            "documentation": "repo-code",
        }

        capability = capability_map.get(classification.detected_intent.value, "repo-code")

        return {
            "capability": capability,
            "intent": classification.detected_intent.value,
            "skill": selection.selected_skill,
            "confidence": selection.confidence,
            "reason": classification.reason,
            "metadata": classification.metadata,
        }
    except Exception:
        return {
            "capability": "repo-code",
            "intent": "feature",
            "skill": "feature",
            "confidence": 0.5,
            "reason": "classification unavailable",
            "metadata": {},
        }

# ============================================================
# Task result endpoint
# ============================================================

@app.get("/api/v1/missions/{mission_id}/result")
def get_task_result(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    """
    Get the final structured result of a completed mission.

    Only available for terminal missions (PASS, FAIL, BLOCKED_EXTERNAL, CANCELLED).
    """
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)

    mission = _load_mission_for(principal, mission_id)

    terminal_statuses = {"PASS", "FAIL", "BLOCKED_EXTERNAL", "BLOCKED", "CANCELLED"}
    if mission.status.value not in terminal_statuses:
        raise HTTPException(409, detail=f"Mission not terminal (status: {mission.status.value})")

    return {
        "mission_id": mission.id,
        "status": mission.status.value,
        "result": mission.result,
        "error_class": mission.error_class,
        "finished_at": mission.finished_at,
        "attempt": mission.attempt,
        "evidence_count": len(mission.evidence),
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
    request: ProductMissionSubmit,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    # Canonical product submit (superset of the legacy shape): top-level
    # repo/model/provider/dry-run/idempotency fields are first-class,
    # and legacy callers that nest them under metadata keep working.
    metadata = dict(request.metadata or {})
    dry_run = bool(request.dry_run or metadata.pop("dry_run", False))
    body_key = request.idempotency_key or metadata.pop("idempotency_key", None)
    for alias in ("repo_path", "repo_ref", "repo"):
        if metadata.get(alias) and "repo_path" not in metadata:
            metadata["repo_path"] = metadata[alias]
    product_request = ProductMissionSubmit(
        goal=request.goal,
        repo_path=request.repo_path or metadata.get("repo_path"),
        repo_ref=request.repo_ref or metadata.get("repo_ref"),
        capability=request.capability,
        constraints=request.constraints or metadata.get("constraints"),
        model=request.model or metadata.get("preferred_model") or metadata.get("model"),
        provider=request.provider or metadata.get("preferred_provider") or metadata.get("provider"),
        metadata=metadata,
        dry_run=dry_run,
        idempotency_key=body_key,
    )
    principal = resolve_principal(authorization)

    if not principal.can("missions.create"):
        raise HTTPException(
            403, detail="missing permission: missions.create"
        )

    _enforce_rate_limit(principal)

    # Governance before any expensive processing (Stage 10.4).
    ok, reason = check_payload_governance(
        goal=product_request.goal,
        metadata=product_request.metadata,
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

    _, _, missions_per_minute = _rate_limits_now()

    allowed, retry_after = rate_limiter.check(
        f"missions:{bucket_key}",
        rpm=missions_per_minute,
        burst=missions_per_minute,
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
    worker = registry.find(product_request.capability)

    if worker is None:
        mission = Mission(
            goal=product_request.goal,
            capability=product_request.capability,
            metadata=dict(product_request.metadata or {}),
            client_id=principal.client_id,
            priority=principal.priority,
            idempotency_key=product_request.idempotency_key,
        )
        mission.status = MissionStatus.blocked
        mission.error_class = "task"
        mission.result = {
            "error": f"No worker for capability: {product_request.capability}"
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
                "capability": product_request.capability,
                "client": principal.name,
            },
        )

        return {
            "id": mission.id,
            "mission_id": mission.id,
            "status": mission.status.value,
            "created_at": mission.created_at,
            "links": {
                "self": f"/api/v1/missions/{mission.id}",
                "evidence": f"/api/v1/missions/{mission.id}/evidence",
                "cancel": f"/api/v1/missions/{mission.id}/cancel",
                "retry": f"/api/v1/missions/{mission.id}/retry",
            },
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

    def _enqueue(mission):
        store.enqueue(mission)
        return mission

    def _wake():
        coordinator = get_coordinator()
        if coordinator is not None:
            coordinator.wake()

    mission, replayed = submit_product_mission(
        store=store,
        audit=audit,
        principal=principal,
        request=product_request,
        header_key=idempotency_key,
        enqueue=_enqueue,
        wake=_wake,
    )
    view = product_view(mission)
    # Legacy keys stay for backward compatibility with Stage 8/9/10.
    view["id"] = mission.id
    view["replayed"] = replayed
    return view


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

    # Clamp pagination parameters to safe bounds
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    start = offset
    end = start + limit

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

    # Isolation first: a client may only ever touch its own
    # mission, so the existence check happens before RBAC. This
    # also lets the missions.cancel.own permission serve client
    # self-service cancellation without widening RBAC.
    if principal.kind == "client":
        mission = store.get(mission_id)

        if not mission or mission.client_id != principal.client_id:
            raise HTTPException(404, "Mission not found")

    if not (
        principal.can("missions.cancel")
        or principal.can("missions.cancel.own")
    ):
        raise HTTPException(
            403, detail="missing permission: missions.cancel"
        )

    _enforce_rate_limit(principal)

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

    # Clamp pagination parameters to safe bounds
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

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

    # Clamp pagination to safe bounds
    limit = max(1, min(int(limit), 500))

    stats = store.outbox_stats()
    pending = store.outbox_pending(limit=limit)

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

# ---------------------------------------------------------
# Worker I: canonical product mission surface
# ---------------------------------------------------------

@app.get("/api/v1/status")
def product_status(authorization: str | None = Header(default=None)):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)
    return {
        "service": "YODAW",
        "status": "READY",
        "auth": auth_mode(),
        "missions": store.status_counts(),
        "workers": registry.status(),
    }


@app.get("/api/v1/capabilities")
def product_capabilities(
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    _enforce_rate_limit(principal)
    return capabilities_view(registry)


@app.post("/api/v1/missions/{mission_id}/retry")
def retry_mission(
    mission_id: str,
    authorization: str | None = Header(default=None),
):
    principal = resolve_principal(authorization)
    if principal.kind == "client":
        probe = store.get(mission_id)
        if not probe or probe.client_id != principal.client_id:
            raise HTTPException(404, "Mission not found")
    if not (
        principal.can("missions.retry")
        or principal.can("missions.create")
        or principal.can("missions.cancel.own")
    ):
        raise HTTPException(403, detail="missing permission: missions.retry")
    _enforce_rate_limit(principal)
    mission = _load_mission_for(principal, mission_id)

    def _wake():
        coordinator = get_coordinator()
        if coordinator is not None:
            coordinator.wake()

    retried = retry_product_mission(
        store=store, audit=audit, principal=principal, mission=mission
    )
    _wake()
    return {
        "mission_id": retried.id,
        "retried_from": mission_id,
        "status": retried.status.value,
        "links": {
            "self": f"/api/v1/missions/{retried.id}",
            "evidence": f"/api/v1/missions/{retried.id}/evidence",
        },
    }


@app.post("/api/v1/product/missions")
def create_product_mission_alias(
    request: ProductMissionSubmit,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return create_product_mission(request, authorization, idempotency_key)


def _create_product_mission_impl(
    request: ProductMissionSubmit,
    authorization: str | None,
    idempotency_key: str | None,
):
    principal = resolve_principal(authorization)
    if not principal.can("missions.create"):
        raise HTTPException(403, detail="missing permission: missions.create")
    _enforce_rate_limit(principal)
    ok, reason = check_payload_governance(
        goal=request.goal,
        metadata={**(request.metadata or {}), "repo": request.repo_path or request.repo_ref},
        config=_governance_config_for(principal),
    )
    if not ok:
        raise HTTPException(413, detail=reason)

    def _enqueue(mission):
        worker = registry.find(mission.capability)
        if worker is None:
            mission.status = MissionStatus.blocked
            mission.error_class = "task"
            mission.result = {
                "error": f"No worker for capability: {mission.capability}"
            }
            mission.finished_at = mission.updated_at
            store.save(mission)
            return mission
        store.enqueue(mission)
        return mission

    def _wake():
        coordinator = get_coordinator()
        if coordinator is not None:
            coordinator.wake()

    mission, replayed = submit_product_mission(
        store=store,
        audit=audit,
        principal=principal,
        request=request,
        header_key=idempotency_key,
        enqueue=_enqueue,
        wake=_wake,
    )
    view = product_view(mission)
    view["replayed"] = replayed
    return view


@app.post("/api/v1/product-missions")
def create_product_mission(
    request: ProductMissionSubmit,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return _create_product_mission_impl(request, authorization, idempotency_key)


try:
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path as _Path

    _product_static = _Path(__file__).resolve().parent / "product" / "static"
    _product_static.mkdir(parents=True, exist_ok=True)
    app.mount("/product", StaticFiles(directory=str(_product_static), html=True), name="product")
except Exception:
    pass
