"""
Structured Planning Layer - Core Models

This module defines the data models for the advanced planning system.
No live LLM required - this is purely structural.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, Union
from uuid import uuid4
from pydantic import BaseModel, Field
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConflictClass(str, Enum):
    NO_CONFLICT = "NO_CONFLICT"
    READ_ONLY_COMPATIBLE = "READ_ONLY_COMPATIBLE"
    POTENTIAL_CONFLICT = "POTENTIAL_CONFLICT"
    HARD_CONFLICT = "HARD_CONFLICT"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class StepStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class Step(BaseModel):
    """A single step in a plan."""
    id: str = Field(default_factory=lambda: f"step_{uuid4().hex[:8]}")
    title: str
    intent: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: dict[str, Any] = Field(default_factory=dict)
    candidate_files: list[str] = Field(default_factory=list)
    read_set: list[str] = Field(default_factory=list)
    write_set: list[str] = Field(default_factory=list)
    shared_resources: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    validation: str = ""
    parallel_safe: bool = True
    parallel_group: Optional[str] = None
    risk: RiskLevel = RiskLevel.LOW
    rollback: str = ""
    evidence_requirements: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None


class Plan(BaseModel):
    """A complete structured plan."""
    goal: str
    summary: str
    steps: list[Step] = Field(default_factory=list)
    dependencies: list[list[str]] = Field(default_factory=list)
    validation_strategy: str = ""
    merge_strategy: str = ""
    risk_summary: dict[str, int] = Field(default_factory=dict)
    revision: int = 0
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    id: str = Field(default_factory=lambda: f"plan_{uuid4().hex[:8]}")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def add_step(self, step: Step) -> None:
        self.steps.append(step)
        self.updated_at = now_iso()

    def get_step(self, step_id: str) -> Optional[Step]:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None

    def get_ready_steps(self, completed_ids: set[str]) -> list[Step]:
        """Get steps whose dependencies are all completed."""
        ready = []
        for step in self.steps:
            if step.status == StepStatus.PENDING:
                if all(dep in completed_ids for dep in step.dependencies):
                    step.status = StepStatus.READY
                    ready.append(step)
        return ready

    def get_parallel_groups(self) -> dict[str, list[Step]]:
        """Group steps by parallel_group."""
        groups: dict[str, list[Step]] = {}
        for step in self.steps:
            if step.parallel_group:
                groups.setdefault(step.parallel_group, []).append(step)
        return groups

    def compute_risk_summary(self) -> dict[str, int]:
        """Count steps by risk level."""
        summary = {level.value: 0 for level in RiskLevel}
        for step in self.steps:
            summary[step.risk.value] += 1
        self.risk_summary = summary
        return summary


class Conflict(BaseModel):
    """Represents a conflict between two steps."""
    step_a: str
    step_b: str
    conflict_class: ConflictClass
    reason: str
    shared_resource: Optional[str] = None


class ValidationResult(BaseModel):
    """Result of plan validation."""
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    topological_order: list[str] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)
    orphans: list[str] = Field(default_factory=list)
    critical_path: list[str] = Field(default_factory=list)
    parallel_groups: dict[str, list[str]] = Field(default_factory=dict)
    conflicts: list[Conflict] = Field(default_factory=list)


class HandoffPackage(BaseModel):
    """Package for handing off a completed plan/step."""
    plan_id: str
    step_id: Optional[str]
    completed_steps: list[str]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    next_steps: list[str] = Field(default_factory=list)
    notes: str = ""


class Checkpoint(BaseModel):
    """A recovery checkpoint."""
    id: str = Field(default_factory=lambda: f"cp_{uuid4().hex[:8]}")
    plan_id: str
    step_id: str
    step_status: StepStatus
    completed_steps: list[str]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    outputs: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)


class RecoveryPlan(BaseModel):
    """Plan for recovering from a failure."""
    original_plan_id: str
    failed_step_id: str
    failure_reason: str
    recovery_steps: list[Step] = Field(default_factory=list)
    rollback_steps: list[Step] = Field(default_factory=list)
    checkpoint: Optional[Checkpoint] = None
    created_at: str = Field(default_factory=now_iso)


class PlanRevision(BaseModel):
    """Record of a plan revision."""
    plan_id: str
    revision: int
    changed_steps: list[str] = Field(default_factory=list)
    added_steps: list[str] = Field(default_factory=list)
    removed_steps: list[str] = Field(default_factory=list)
    reason: str = ""
    created_at: str = Field(default_factory=now_iso)
    previous_revision: Optional[int] = None