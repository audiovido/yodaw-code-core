from enum import Enum
from typing import Any
from uuid import uuid4
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionStatus(str, Enum):
    queued = "QUEUED"
    running = "RUNNING"
    verifying = "VERIFYING"
    repairing = "REPAIRING"
    recovering = "RECOVERING"
    passed = "PASS"
    failed = "FAIL"
    blocked = "BLOCKED"
    cancelled = "CANCELLED"


TERMINAL_STATUSES = {
    MissionStatus.passed,
    MissionStatus.failed,
    MissionStatus.blocked,
    MissionStatus.cancelled,
}


class MissionCreate(BaseModel):
    goal: str
    capability: str = "code"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ClientCreate(BaseModel):
    name: str
    priority: int = 5
    max_concurrent_missions: int | None = None


class ClientPriorityUpdate(BaseModel):
    priority: int


class Mission(BaseModel):
    id: str = Field(default_factory=lambda: f"m_{uuid4().hex[:12]}")
    goal: str
    capability: str
    status: MissionStatus = MissionStatus.queued
    worker: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    # Stage 8 runtime fields. All defaulted so Stage 7 payloads
    # and requests keep loading without migration of stored JSON.
    attempt: int = 0
    max_attempts: int = 1
    claimed_by: str | None = None
    claimed_at: str | None = None
    started_at: str | None = None
    heartbeat_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False

    # Stage 9 multi-tenancy. Default priority keeps single-tenant
    # (Stage 8) ordering identical; client_id is None for calls
    # made without a client identity (local-dev / shared key).
    client_id: str | None = None
    priority: int = 5
