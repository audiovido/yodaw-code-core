"""Scheduler data models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_task_id() -> str:
    return f"task_{uuid4().hex[:12]}"


class TaskState(str, Enum):
    queued = "QUEUED"
    blocked = "BLOCKED"
    ready = "READY"
    assigned = "ASSIGNED"
    running = "RUNNING"
    retry_wait = "RETRY_WAIT"
    recovering = "RECOVERING"
    completed = "COMPLETED"
    failed = "FAILED"
    blocked_external = "BLOCKED_EXTERNAL"
    cancelled = "CANCELLED"


TERMINAL_TASK_STATES = frozenset(
    {TaskState.completed, TaskState.failed, TaskState.cancelled}
)

CLAIMABLE_STATES = frozenset(
    {TaskState.ready, TaskState.recovering}
)


class TaskSpec(BaseModel):
    task_id: str = Field(default_factory=new_task_id)
    goal: str = ""
    capability: str = "code"
    priority: int = 5
    dependencies: list[str] = Field(default_factory=list)
    max_retries: int = 3
    preemptible: bool = False
    payload: dict = Field(default_factory=dict)
    state: TaskState = TaskState.queued
    retries: int = 0
    wait_rounds: int = 0
    created_seq: int = 0
    created_at: str = Field(default_factory=now_iso)
    lease_owner: str | None = None
    lease_expiry: str | None = None
    heartbeat_at: str | None = None
    next_retry_at: str | None = None
    last_error_type: str | None = None
    last_error_message: str = ""
    checkpoint: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)


class WorkerSpec(BaseModel):
    worker_id: str
    concurrency_limit: int = 1
    capabilities: list[str] = Field(default_factory=list)
    heartbeat_at: str = Field(default_factory=now_iso)


class TaskOutput(BaseModel):
    task_id: str
    patch_id: str = ""
    touched_files: list[str] = Field(default_factory=list)
    worktree_ref: str = ""
    summary: str = ""


class MergeStatus(str, Enum):
    pending = "PENDING"
    ready = "READY"
    conflict = "CONFLICT"
    needs_review = "NEEDS_REVIEW"
    merged = "MERGED"
    resolved = "RESOLVED"


class MergeCandidate(BaseModel):
    candidate_id: str = Field(default_factory=lambda: f"mc_{uuid4().hex[:8]}")
    task_id: str
    patch_id: str = ""
    touched_files: list[str] = Field(default_factory=list)
    worktree_ref: str = ""
    status: MergeStatus = MergeStatus.pending
    created_at: str = Field(default_factory=now_iso)


class SchedulingEvent(BaseModel):
    seq: int = 0
    task_id: str
    event_type: str
    created_at: str = Field(default_factory=now_iso)
    data: dict = Field(default_factory=dict)
