from enum import Enum
from typing import Any, Optional
from uuid import uuid4
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionStatus(str, Enum):
    queued = "QUEUED"
    observing = "OBSERVING"
    planning = "PLANNING"
    running = "RUNNING"
    executing = "EXECUTING"
    verifying = "VERIFYING"
    repairing = "REPAIRING"
    recovering = "RECOVERING"
    passed = "PASS"
    failed = "FAIL"
    blocked = "BLOCKED"
    blocked_external = "BLOCKED_EXTERNAL"
    cancelled = "CANCELLED"


TERMINAL_STATUSES = {
    MissionStatus.passed,
    MissionStatus.failed,
    MissionStatus.blocked,
    MissionStatus.blocked_external,
    MissionStatus.cancelled,
}


class MissionCreate(BaseModel):
    goal: str
    capability: str = "code"
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Worker I product surface (all optional; legacy callers omit).
    repo_path: Optional[str] = None
    repo_ref: Optional[str] = None
    constraints: Optional[dict[str, Any]] = None
    model: Optional[dict[str, Any]] = None
    provider: Optional[dict[str, Any]] = None
    dry_run: bool = False
    idempotency_key: Optional[str] = None


class ClientCreate(BaseModel):
    name: str
    priority: int = 5
    max_concurrent_missions: Optional[int] = None


class ClientPriorityUpdate(BaseModel):
    priority: int


class ClientQuotaUpdate(BaseModel):
    max_concurrent_missions: Optional[int] = None


class AdminCreate(BaseModel):
    name: str
    role: str = "operator"


class AdminRoleUpdate(BaseModel):
    role: str


class AuditPruneRequest(BaseModel):
    keep_days: int
    archive_path: Optional[str] = None


class Mission(BaseModel):
    id: str = Field(default_factory=lambda: f"m_{uuid4().hex[:12]}")
    goal: str
    capability: str
    status: MissionStatus = MissionStatus.queued
    worker: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    # Worker I product lifecycle fields (defaulted; old payloads load).
    # attempt_lineage records mission ids retried from this mission.
    # retried_from_id records the mission this one retried.
    # idempotency_key makes resubmission exactly-once per tenant.
    # error_class is task | provider | blocked_external | cancelled.
    attempt_lineage: list[str] = Field(default_factory=list)
    retried_from_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    error_class: Optional[str] = None

    # Stage 8 runtime fields. All defaulted so Stage 7 payloads
    # and requests keep loading without migration of stored JSON.
    attempt: int = 0
    max_attempts: int = 1
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    started_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_requested: bool = False

    # Stage 9 multi-tenancy. Default priority keeps single-tenant
    # (Stage 8) ordering identical; client_id is None for calls
    # made without a client identity (local-dev / shared key).
    client_id: Optional[str] = None
    priority: int = 5
