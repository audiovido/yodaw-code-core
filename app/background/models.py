"""Kodgar background Task API: canonical models.

This module is the single source of truth for the task lifecycle that
the web UI, the CLI, the executor registry, and the verifier all
speak. Nothing here executes anything; it defines the durable shapes.

Design rules:

- The state machine is explicit and closed. ``TaskState`` is the only
  vocabulary the API and the UI render.
- The planner contract is strict: a planner response that does not
  validate never proceeds. ``PlanResult`` forbids unknown keys, so a
  planner that invents fields fails loudly instead of silently.
- Terminal success (``COMPLETED``) is *never* derived from model text;
  it is only reachable after the verifier attests real filesystem and
  git evidence (see ``app.background.verifier``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_task_id() -> str:
    return f"t_{uuid4().hex[:12]}"


class TaskState(str, Enum):
    """The task state machine.

    Forward path::

        QUEUED -> PLANNING -> PLANNED -> PREPARING -> CODING
               -> TESTING -> VERIFYING -> COMMITTING -> COMPLETED

    Off-path terminal states: BLOCKED, FAILED, CANCELLED.
    """

    queued = "QUEUED"
    planning = "PLANNING"
    planned = "PLANNED"
    preparing = "PREPARING"
    coding = "CODING"
    testing = "TESTING"
    verifying = "VERIFYING"
    committing = "COMMITTING"
    completed = "COMPLETED"
    blocked = "BLOCKED"
    failed = "FAILED"
    cancelled = "CANCELLED"


TERMINAL_STATES = {
    TaskState.completed,
    TaskState.blocked,
    TaskState.failed,
    TaskState.cancelled,
}

# Progress is *derived from real pipeline stages*, never from a timer.
# Each value is the completion percentage of the stage that is about
# to run, so a task sits at a given percentage only while it is
# actually inside that stage.
STAGE_PROGRESS: dict[TaskState, int] = {
    TaskState.queued: 0,
    TaskState.planning: 5,
    TaskState.planned: 20,
    TaskState.preparing: 28,
    TaskState.coding: 40,
    TaskState.testing: 70,
    TaskState.verifying: 84,
    TaskState.committing: 94,
    TaskState.completed: 100,
    TaskState.blocked: 100,
    TaskState.failed: 100,
    TaskState.cancelled: 100,
}

EXECUTOR_IDS = ("claude-code", "codex", "grok-cli", "kodgar-native")

ExecutorId = Literal["claude-code", "codex", "grok-cli", "kodgar-native"]


class ExecutorPreference(str, Enum):
    auto = "auto"


class PlannedEdit(BaseModel):
    """One deterministic file mutation requested by the planner.

    ``find`` empty + target absent means "create the file with
    ``replace`` as its exact content" — the same contract the existing
    ``app.workers.edit_engine`` implements, so a plan can be executed
    without a second edit dialect.
    """

    model_config = ConfigDict(extra="forbid")

    target_file: str
    find: str = ""
    replace: str = ""


class PlanResult(BaseModel):
    """Strict Grok Architect output.

    Mirrors the product contract exactly and forbids extra keys: a
    malformed planner response raises ``ValidationError`` at the
    boundary and the task fails with ``PlannerError``.
    """

    model_config = ConfigDict(extra="forbid")

    task_type: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    languages: list[str] = Field(min_length=1)
    frameworks: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    executor: ExecutorId
    reason: str = Field(min_length=1)
    plan: list[str] = Field(min_length=1)
    acceptance: list[str] = Field(min_length=1)

    # Optional execution hints. Present only when the planner can be
    # precise; every one of them is validated before use.
    target_files: list[str] = Field(default_factory=list)
    edits: list[PlannedEdit] = Field(default_factory=list)
    test_command: Optional[str] = None
    build_command: Optional[str] = None
    expected_scope: list[str] = Field(default_factory=list)

    @field_validator("languages", "frameworks", "tools", "plan", "acceptance")
    @classmethod
    def _strip_blank(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @field_validator("target_files", "expected_scope")
    @classmethod
    def _strip_paths(cls, value: list[str]) -> list[str]:
        """Reject anything that is not a plain repo-relative path.

        A planner naming ``../secrets.env``, an absolute path, or a
        home-relative path must fail validation rather than be silently
        rewritten: the plan's file list is what the verifier later
        enforces, so a sanitized-but-wrong path would weaken it.
        Leading ``./`` is the only normalization applied, and dotted
        directory names (``.github/...``) survive it.
        """
        cleaned = []
        for item in value:
            if not item or not item.strip():
                continue
            candidate = item.strip()
            while candidate.startswith("./"):
                candidate = candidate[2:]
            parts = [part for part in candidate.split("/") if part not in ("", ".")]
            if (
                not candidate
                or candidate.startswith(("/", "~"))
                or any(part == ".." for part in parts)
            ):
                raise ValueError(f"unsafe planned path: {item!r}")
            cleaned.append("/".join(parts))
        return cleaned


class TaskRequest(BaseModel):
    """``POST /api/v1/tasks`` body."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, pattern=r"\S")
    repo: Optional[str] = None
    project_id: Optional[str] = None
    preferences: dict[str, Any] = Field(default_factory=dict)
    # Optional per-task wall-clock budget. Some repositories have
    # legitimately slow suites, so a caller may need more than the
    # engine default; bounded so a request can never disable the
    # timeout entirely.
    timeout_seconds: Optional[int] = Field(default=None, ge=60, le=14400)

    @field_validator("goal")
    @classmethod
    def _bounded_goal(cls, value: str) -> str:
        if len(value) > 8000:
            raise ValueError("goal exceeds 8000 characters")
        return value


class TaskEvent(BaseModel):
    """One durable, ordered event in a task's life."""

    seq: int
    task_id: str
    type: str
    ts: str
    data: dict[str, Any] = Field(default_factory=dict)


class ExecutorInfo(BaseModel):
    """Normalized executor descriptor used by the registry and the API."""

    id: str
    label: str
    kind: str
    available: bool
    detail: str = ""
    capabilities: list[str] = Field(default_factory=list)
    command: Optional[str] = None
    version: Optional[str] = None


class ExecutorHealth(BaseModel):
    """Structured, evidence-based executor health.

    ``available == binary exists`` was a lie that routed real tasks
    into broken model configurations. Health is a ladder of distinct
    facts: installed -> authenticated -> model_available ->
    inference_ok. Anything still unknown is ``None``, never silently
    assumed good; known-fatal ``error_type`` values (quota, upstream
    forbiddance, missing model, auth) make the executor ineligible for
    routing even though its binary exists.
    """

    id: str
    label: str = ""
    kind: str = "cli"
    installed: bool = False
    authenticated: Optional[bool] = None
    model_available: Optional[bool] = None
    inference_ok: Optional[bool] = None
    healthy: bool = False
    eligible: bool = False
    latency_ms: Optional[int] = None
    error_type: Optional[str] = None
    detail: str = ""
    checked_at: str = Field(default_factory=now_iso)
    cooldown_until: Optional[str] = None
    circuit_open: bool = False
    capabilities: list[str] = Field(default_factory=list)
    version: Optional[str] = None


class ExecutionOutcome(BaseModel):
    """What an executor reports after touching a real worktree.

    ``succeeded`` means *the tool exited cleanly*; it is explicitly not
    a statement about whether the task is done. Only the verifier may
    make that claim.
    """

    succeeded: bool
    exit_code: Optional[int] = None
    summary: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    error: Optional[dict[str, Any]] = None
    files_touched: list[str] = Field(default_factory=list)


class VerificationCheck(BaseModel):
    name: str
    status: Literal["PASS", "FAIL", "SKIPPED"]
    detail: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)


class VerificationReport(BaseModel):
    """The verifier's authoritative verdict."""

    status: Literal["PASS", "FAIL"]
    checks: list[VerificationCheck] = Field(default_factory=list)
    files_changed: int = 0
    touched_files: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    build_status: Literal["PASS", "FAIL", "SKIPPED"] = "SKIPPED"
    commit_sha: Optional[str] = None
    diff: str = ""
    detail: str = ""

    def failed_checks(self) -> list[VerificationCheck]:
        return [c for c in self.checks if c.status == "FAIL"]


class TaskRecord(BaseModel):
    """The durable task row, as returned by the API."""

    id: str = Field(default_factory=new_task_id)
    goal: str
    repo: Optional[str] = None
    project_id: Optional[str] = None
    state: TaskState = TaskState.queued
    progress: int = 0
    current_step: str = "queued"
    planner: str = "grok"
    planner_backend: Optional[str] = None
    executor: Optional[str] = None
    executor_reason: Optional[str] = None
    plan: Optional[PlanResult] = None
    preferences: dict[str, Any] = Field(default_factory=dict)

    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    heartbeat_at: Optional[str] = None

    files_changed: int = 0
    touched_files: list[str] = Field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    build_status: str = "UNKNOWN"
    verify_status: str = "UNKNOWN"
    verification: Optional[VerificationReport] = None

    branch: Optional[str] = None
    worktree: Optional[str] = None
    base_sha: Optional[str] = None
    commit_sha: Optional[str] = None
    pid: Optional[int] = None
    cancel_requested: bool = False
    timeout_seconds: Optional[int] = None
    error: Optional[dict[str, Any]] = None
    result: dict[str, Any] = Field(default_factory=dict)
    log_lines: int = 0

    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or now_iso()
        try:
            start = datetime.fromisoformat(self.started_at)
            stop = datetime.fromisoformat(end)
        except ValueError:
            return 0.0
        return max(0.0, (stop - start).total_seconds())

    def public_view(self) -> dict[str, Any]:
        """The ``GET /api/v1/tasks/{id}`` payload.

        Deliberately flat and UI-shaped: the terminal inspector renders
        exactly these fields, so the contract and the screen cannot
        drift apart.
        """
        return {
            "id": self.id,
            "goal": self.goal,
            "repo": self.repo,
            "project_id": self.project_id,
            "state": self.state.value,
            "progress": self.progress,
            "current_step": self.current_step,
            "executor": self.executor,
            "executor_reason": self.executor_reason,
            "planner": self.planner,
            "planner_backend": self.planner_backend,
            "plan": self.plan.model_dump() if self.plan else None,
            "started_at": self.started_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.elapsed_seconds(), 1),
            "files_changed": self.files_changed,
            "touched_files": self.touched_files,
            "tests": {
                "passed": self.tests_passed,
                "failed": self.tests_failed,
            },
            "build_status": self.build_status,
            "verify_status": self.verify_status,
            "verification": (
                self.verification.model_dump() if self.verification else None
            ),
            "branch": self.branch,
            "worktree": self.worktree,
            "base_sha": self.base_sha,
            "commit_sha": self.commit_sha,
            "pid": self.pid,
            "cancel_requested": self.cancel_requested,
            "timeout_seconds": self.timeout_seconds,
            "error": self.error,
            "result": self.result,
            "is_terminal": self.state in TERMINAL_STATES,
        }


class PlannerError(RuntimeError):
    """The architect produced output that must not be executed."""


class TaskCancelled(RuntimeError):
    """Raised inside a running task when cancellation was requested."""


class TaskTimeout(RuntimeError):
    """Raised when a task exceeds its wall-clock budget."""


# ------------------------------------------------------------ error classes
# How an executor failure should be handled. Classification is derived
# from structured CLI/provider evidence (status codes, named error
# types) first, with string patterns only as the fallback net.
RETRY_SAME_EXECUTOR = "RETRY_SAME_EXECUTOR"
RETRY_DIFFERENT_EXECUTOR = "RETRY_DIFFERENT_EXECUTOR"
NON_RETRYABLE_TASK_ERROR = "NON_RETRYABLE_TASK_ERROR"
VERIFICATION_FAILURE = "VERIFICATION_FAILURE"

# Fatal-for-executor error types: seeing one of these means the
# executor cannot run *any* task right now, so the circuit opens.
FATAL_EXECUTOR_ERRORS = frozenset(
    {
        "not_installed",
        "auth_failed",
        "model_not_found",
        "quota_exhausted",
        "upstream_forbidden",
        "headless_unsupported",
    }
)

# Error types that are the *task's* fault, not the executor's:
# falling back to another executor would fail the same way.
TASK_FAULT_ERRORS = frozenset(
    {
        "invalid_repo",
        "invalid_request",
        "impossible_acceptance",
        "security_rejection",
    }
)


def error_class_action(error_type: str) -> str:
    """The routing decision for a classified executor error.

    Fatal-for-executor types (and the conservative unknown) mean "try
    a different executor"; transient types mean "same executor may
    succeed on retry"; task-fault types mean "no executor would do
    better".
    """
    if error_type in {"transient_timeout", "transient_overload"}:
        return RETRY_SAME_EXECUTOR
    if error_type in FATAL_EXECUTOR_ERRORS or error_type == "unknown":
        return RETRY_DIFFERENT_EXECUTOR
    return NON_RETRYABLE_TASK_ERROR
