"""Durable job model for the long-running task supervisor."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    return f"job_{uuid4().hex[:12]}"


class JobState(str, Enum):
    queued = "QUEUED"
    claimed = "CLAIMED"
    running = "RUNNING"
    paused = "PAUSED"
    recovering = "RECOVERING"
    blocked_external = "BLOCKED_EXTERNAL"
    failed = "FAILED"
    cancelled = "CANCELLED"
    passed = "PASS"


TERMINAL_STATES = frozenset(
    {JobState.failed, JobState.cancelled, JobState.passed}
)


class Stage(str, Enum):
    observe = "OBSERVE"
    plan = "PLAN"
    execute_step = "EXECUTE_STEP"
    verify = "VERIFY"
    recover = "RECOVER"


class Checkpoint(BaseModel):
    job_id: str
    stage: Stage
    step: int = 0
    attempt: int = 0
    evidence_refs: list[str] = Field(default_factory=list)
    committed_actions: list[str] = Field(default_factory=list)
    summary: str = ""
    created_at: str = Field(default_factory=now_iso)


class SupervisorEvent(BaseModel):
    seq: int = 0
    job_id: str
    event_type: str
    attempt: int = 0
    created_at: str = Field(default_factory=now_iso)
    data: dict = Field(default_factory=dict)


class JobRecord(BaseModel):
    job_id: str = Field(default_factory=new_job_id)
    mission_id: str | None = None
    state: JobState = JobState.queued
    stage: Stage | None = None
    step: int = 0
    attempt: int = 0
    max_retries: int = 3
    created_at: str = Field(default_factory=now_iso)
    started_at: str | None = None
    heartbeat_at: str | None = None
    updated_at: str = Field(default_factory=now_iso)
    finished_at: str | None = None
    worker_id: str | None = None
    lease_owner: str | None = None
    lease_expiry: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    last_error_type: str | None = None
    last_error_message: str | None = None
    retry_reason: str | None = None
    retries: int = 0
    next_retry_at: str | None = None
    pause_requested: bool = False
    cancel_requested: bool = False
    result: dict = Field(default_factory=dict)
