"""Worker I canonical product API (v1 mission surface).

Thin adapter over the existing hardened mission API in app.main.
No business logic is duplicated: product submit maps to one
Mission, idempotency is enforced before enqueue, and retry keeps
prior attempt evidence with traceable lineage.
"""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

from app.api.auth import Principal, resolve_principal
from app.core.models import Mission, MissionStatus
from app.mission.facade import MAX_RETRY_ATTEMPTS, retry_allowed
from app.storage.sqlite_store import DuplicateMission
from app.tenants.redact import redact


class ProductMissionSubmit(BaseModel):
    goal: str = Field(min_length=1, pattern=r"\S")
    repo_path: str | None = None
    repo_ref: str | None = None
    capability: str = Field(default="repo-code", min_length=1)
    constraints: dict[str, Any] | None = None
    model: dict[str, Any] | None = None
    provider: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False
    idempotency_key: str | None = None


def tenant_scope(principal: Principal) -> str:
    return principal.client_id or principal.admin_id or (
        f"{principal.kind}:{principal.name}"
    )


def mission_links(mission_id: str) -> dict[str, str]:
    base = f"/api/v1/missions/{mission_id}"
    return {
        "self": base,
        "evidence": f"{base}/evidence",
        "cancel": f"{base}/cancel",
        "retry": f"{base}/retry",
    }


def to_product_status(status: MissionStatus) -> str:
    mapping = {
        MissionStatus.queued: "QUEUED",
        MissionStatus.observing: "OBSERVING",
        MissionStatus.planning: "PLANNING",
        MissionStatus.running: "EXECUTING",
        MissionStatus.executing: "EXECUTING",
        MissionStatus.verifying: "VERIFYING",
        MissionStatus.repairing: "RECOVERING",
        MissionStatus.recovering: "RECOVERING",
        MissionStatus.passed: "PASS",
        MissionStatus.failed: "FAIL",
        MissionStatus.blocked: "BLOCKED",
        MissionStatus.blocked_external: "BLOCKED_EXTERNAL",
        MissionStatus.cancelled: "CANCELLED",
    }
    return mapping.get(status, status.value)


def product_view(mission: Mission) -> dict:
    """Unified mission record with all observable state."""
    # Redact secrets from evidence and result for API visibility
    safe_evidence = redact(mission.evidence) if mission.evidence else []
    safe_result = redact(mission.result) if mission.result else {}
    
    return {
        "id": mission.id,
        "mission_id": mission.id,
        "status": to_product_status(mission.status),
        "created_at": mission.created_at,
        "updated_at": mission.updated_at,
        "finished_at": mission.finished_at,
        "attempt": mission.attempt,
        "max_attempts": mission.max_attempts,
        "attempts": mission.metadata.get("product_attempts", 1),
        "attempt_lineage": mission.attempt_lineage,
        "retried_from_id": mission.retried_from_id,
        "error_class": mission.error_class,
        "result": safe_result,
        "evidence": safe_evidence,
        "claimed_by": mission.claimed_by,
        "claimed_at": mission.claimed_at,
        "started_at": mission.started_at,
        "heartbeat_at": mission.heartbeat_at,
        "cancel_requested": mission.cancel_requested,
        "worker": mission.worker,
        "priority": mission.priority,
        "client_id": mission.client_id,
        "idempotency_key": mission.idempotency_key,
        "links": mission_links(mission.id),
    }


def resolve_idempotency_key(
    body_key: str | None, header_key: str | None
) -> str | None:
    key = (body_key or header_key or "").strip()
    return key or None


def build_metadata(request: ProductMissionSubmit) -> dict:
    metadata = dict(request.metadata or {})
    if request.repo_path and "repo_path" not in metadata:
        metadata["repo_path"] = request.repo_path
    if request.repo_ref and "repo_ref" not in metadata:
        metadata["repo_ref"] = request.repo_ref
    if request.constraints is not None:
        metadata["constraints"] = request.constraints
    if request.model is not None:
        metadata["preferred_model"] = request.model
    if request.provider is not None:
        metadata["preferred_provider"] = request.provider
    if request.dry_run:
        metadata["dry_run"] = True
    metadata["product_attempts"] = metadata.get("product_attempts", 1)
    metadata["max_retries"] = metadata.get(
        "max_retries", MAX_RETRY_ATTEMPTS - 1
    )
    return metadata


def check_idempotent_replay(
    store, scope: str, key: str | None, principal: Principal
) -> Mission | None:
    if not key:
        return None
    mission_id = store.idempotency_lookup(scope, key)
    if not mission_id:
        return None
    mission = store.get(mission_id)
    if mission is None:
        # No dangling pointers: claim+insert is one tx, so a
        # missing row means a legacy/crashed mapping. Resubmit
        # fresh instead of 409ing.
        return None
    if principal.kind == "client" and mission.client_id != principal.client_id:
        raise HTTPException(404, "Mission not found")
    return mission


def submit_product_mission(
    *,
    store,
    audit,
    principal: Principal,
    request: ProductMissionSubmit,
    header_key: str | None,
    enqueue,
    wake,
) -> tuple[Mission, bool]:
    """Submit exactly once; returns (mission, replayed)."""
    key = resolve_idempotency_key(request.idempotency_key, header_key)
    scope = tenant_scope(principal)
    if key:
        replay = check_idempotent_replay(store, scope, key, principal)
        if replay is not None:
            return replay, True
    metadata = build_metadata(request)
    mission = Mission(
        goal=request.goal,
        capability=request.capability,
        metadata=metadata,
        client_id=principal.client_id,
        priority=principal.priority,
        idempotency_key=key,
    )
    claimed_id = mission.id
    if key and hasattr(store, "submit_idempotent_mission"):
        if request.dry_run:
            mission.status = MissionStatus.passed
            mission.result = {"dry_run": True, "goal": request.goal}
            mission.error_class = "task"
            mission.finished_at = mission.updated_at
            try:
                stored, replayed = store.submit_idempotent_mission(
                    mission, tenant_scope=scope, idempotency_key=key
                )
            except DuplicateMission as exc:
                raise HTTPException(
                    status_code=409,
                    detail=str(exc),
                    headers={"X-YODAW-Active-Mission": exc.mission_id or ""},
                )
            if not replayed and stored.id == mission.id:
                try:
                    store.save(mission)
                except Exception:
                    pass
                stored = store.get(mission.id) or mission
            mission = stored
            audit.append(
                client_id=principal.client_id,
                actor=principal.name,
                action="mission.created",
                mission_id=mission.id,
                data={
                    "goal": mission.goal,
                    "capability": mission.capability,
                    "product": True,
                    "dry_run": request.dry_run,
                },
            )
            wake()
            return mission, replayed
        try:
            stored, replayed = store.submit_idempotent_mission(
                mission, tenant_scope=scope, idempotency_key=key
            )
        except DuplicateMission as exc:
            raise HTTPException(
                status_code=409,
                detail=str(exc),
                headers={"X-YODAW-Active-Mission": exc.mission_id or ""},
            )
        if replayed:
            if (
                principal.kind == "client"
                and stored.client_id != principal.client_id
            ):
                raise HTTPException(404, "Mission not found")
            return stored, True
        mission = stored
        audit.append(
            client_id=principal.client_id,
            actor=principal.name,
            action="mission.created",
            mission_id=mission.id,
            data={
                "goal": mission.goal,
                "capability": mission.capability,
                "product": True,
                "dry_run": request.dry_run,
            },
        )
        wake()
        return mission, False
    if key:
        claimed_id = store.idempotency_claim(scope, key, mission.id)
        if claimed_id != mission.id:
            existing = store.get(claimed_id)
            if existing is None:
                raise HTTPException(409, "idempotency conflict without mission")
            if (
                principal.kind == "client"
                and existing.client_id != principal.client_id
            ):
                raise HTTPException(404, "Mission not found")
            return existing, True
    if request.dry_run:
        mission.status = MissionStatus.passed
        mission.result = {"dry_run": True, "goal": request.goal}
        mission.error_class = "task"
        mission.finished_at = mission.updated_at
        store.save(mission)
    else:
        try:
            enqueue(mission)
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
            "product": True,
            "dry_run": request.dry_run,
        },
    )
    wake()
    # Return the enqueue-time snapshot, not a re-read: the coordinator
    # may legitimately claim the mission between enqueue and response,
    # and POST must report the submission outcome (QUEUED), never a
    # concurrently-advanced execution state.
    return mission, False


def retry_product_mission(*, store, audit, principal, mission: Mission) -> Mission:
    allowed, reason = retry_allowed(mission)
    if not allowed:
        raise HTTPException(409, reason)
    new_metadata = dict(mission.metadata)
    attempts = int(new_metadata.get("product_attempts", 1)) + 1
    new_metadata["product_attempts"] = attempts
    retried = Mission(
        goal=mission.goal,
        capability=mission.capability,
        metadata=new_metadata,
        client_id=mission.client_id,
        priority=mission.priority,
        retried_from_id=mission.id,
    )
    prior = list(mission.evidence)
    prior.append(
        {
            "type": "retry_lineage",
            "retried_from": mission.id,
            "attempt": attempts,
        }
    )
    retried.evidence = prior
    try:
        store.enqueue(retried)
    except DuplicateMission as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-YODAW-Active-Mission": exc.mission_id or ""},
        )
    source = store.get(mission.id)
    if source is not None:
        lineage = list(source.attempt_lineage)
        lineage.append(retried.id)
        source.attempt_lineage = lineage
        store.save(source)
    audit.append(
        client_id=principal.client_id,
        actor=principal.name,
        action="mission.retried",
        mission_id=retried.id,
        data={"retried_from": mission.id, "attempt": attempts},
    )
    return store.get(retried.id) or retried


def capabilities_view(registry) -> dict:
    return {
        "capabilities": [
            {"name": item.get("name"), "capabilities": item.get("capabilities", [])}
            for item in registry.status()
        ],
        "mission_statuses": [
            "QUEUED",
            "OBSERVING",
            "PLANNING",
            "EXECUTING",
            "VERIFYING",
            "RECOVERING",
            "PASS",
            "FAIL",
            "BLOCKED_EXTERNAL",
            "CANCELLED",
        ],
    }


def resolve_bearer(authorization: str | None) -> Principal:
    return resolve_principal(authorization)


def header_idempotency_key(
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> tuple[str | None, str | None]:
    return authorization, idempotency_key
