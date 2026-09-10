"""Governed improvement proposals for the self-improving loop.

Every loop state change is an explicit transition on an
``ImprovementProposal`` record; there is no silent self-modification.
Proposals carry full metadata (expected impact, risk score, validation
plan, benchmark before/after, regression delta) and move through
approval states (DRAFT -> PENDING_REVIEW -> APPROVED | REJECTED, with
APPROVED -> ACTIVATED or APPROVED -> ROLLED_BACK). Rejections are
retained with reasons; nothing is ever deleted before activation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class ProposalStatus(str, Enum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ACTIVATED = "ACTIVATED"
    ROLLED_BACK = "ROLLED_BACK"


class ProposalKind(str, Enum):
    SKILL = "SKILL"
    POLICY = "POLICY"


TERMINAL_STATES = {
    ProposalStatus.REJECTED,
    ProposalStatus.ROLLED_BACK,
}

# Explicit transitions only; anything else raises. ACTIVATED and
# ROLLED_BACK additionally require the privileged ActivationStore
# path (see store.transition's `via` argument).
TRANSITIONS: dict[ProposalStatus, set[ProposalStatus]] = {
    ProposalStatus.DRAFT: {ProposalStatus.PENDING_REVIEW},
    ProposalStatus.PENDING_REVIEW: {
        ProposalStatus.APPROVED,
        ProposalStatus.REJECTED,
    },
    ProposalStatus.APPROVED: {
        ProposalStatus.ACTIVATED,
        ProposalStatus.ROLLED_BACK,
    },
    ProposalStatus.ACTIVATED: {ProposalStatus.ROLLED_BACK},
    ProposalStatus.REJECTED: set(),
    ProposalStatus.ROLLED_BACK: set(),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def proposal_id_for(kind: str, cluster_id: str, content_hash: str) -> str:
    digest = hashlib.sha1(
        f"{kind}:{cluster_id}:{content_hash}".encode("utf-8")
    ).hexdigest()
    return f"imp_{digest[:12]}"


def is_terminal(status: ProposalStatus) -> bool:
    return status in TERMINAL_STATES


def can_transition(frm: ProposalStatus, to: ProposalStatus) -> bool:
    return to in TRANSITIONS.get(frm, set())


@dataclass
class BenchmarkDelta:
    """Before/after benchmark evidence attached to a proposal."""

    suite: str
    before_score: float
    after_score: float
    cases_run: int = 0

    @property
    def improvement(self) -> float:
        return self.after_score - self.before_score

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "before_score": self.before_score,
            "after_score": self.after_score,
            "improvement": self.improvement,
            "cases_run": self.cases_run,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkDelta":
        return cls(
            suite=str(data.get("suite", "")),
            before_score=float(data.get("before_score", 0.0)),
            after_score=float(data.get("after_score", 0.0)),
            cases_run=int(data.get("cases_run", 0)),
        )


@dataclass
class RegressionDelta:
    """Regression-check outcome for a proposal."""

    suite: str
    failures_before: int
    failures_after: int
    passed: bool = False

    @property
    def delta(self) -> int:
        return self.failures_after - self.failures_before

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "failures_before": self.failures_before,
            "failures_after": self.failures_after,
            "delta": self.delta,
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegressionDelta":
        return cls(
            suite=str(data.get("suite", "")),
            failures_before=int(data.get("failures_before", 0)),
            failures_after=int(data.get("failures_after", 0)),
            passed=bool(data.get("passed", False)),
        )


@dataclass
class ImprovementProposal:
    """One governed skill/policy improvement candidate."""

    id: str
    kind: str
    title: str
    description: str
    cluster_id: str | None = None
    source: str = "self-improving-loop"
    status: ProposalStatus = ProposalStatus.DRAFT
    expected_impact: str = ""
    risk_score: float = 0.0
    validation_plan: list[str] = field(default_factory=list)
    content: dict[str, Any] = field(default_factory=dict)
    content_hash: str = ""
    benchmark: Optional[BenchmarkDelta] = None
    regression: Optional[RegressionDelta] = None
    submitted_by: str = "loop"
    reviewed_by: str | None = None
    review_note: str | None = None
    rejection_reason: str | None = None
    duplicate_of: str | None = None
    version: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def touch(self) -> None:
        self.updated_at = now_iso()

    def to_dict(self) -> dict[str, Any]:
        status = self.status.value if isinstance(self.status, Enum) else str(self.status)
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "description": self.description,
            "cluster_id": self.cluster_id,
            "source": self.source,
            "status": status,
            "expected_impact": self.expected_impact,
            "risk_score": self.risk_score,
            "validation_plan": list(self.validation_plan),
            "content": dict(self.content),
            "content_hash": self.content_hash,
            "benchmark": self.benchmark.to_dict() if self.benchmark else None,
            "regression": self.regression.to_dict() if self.regression else None,
            "submitted_by": self.submitted_by,
            "reviewed_by": self.reviewed_by,
            "review_note": self.review_note,
            "rejection_reason": self.rejection_reason,
            "duplicate_of": self.duplicate_of,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImprovementProposal":
        raw_status = data.get("status", ProposalStatus.DRAFT.value)
        status = (
            raw_status
            if isinstance(raw_status, ProposalStatus)
            else ProposalStatus(str(raw_status))
        )
        benchmark = data.get("benchmark")
        regression = data.get("regression")
        return cls(
            id=str(data.get("id", "")),
            kind=str(data.get("kind", ProposalKind.SKILL.value)),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            cluster_id=data.get("cluster_id"),
            source=str(data.get("source", "self-improving-loop")),
            status=status,
            expected_impact=str(data.get("expected_impact", "")),
            risk_score=float(data.get("risk_score", 0.0)),
            validation_plan=list(data.get("validation_plan", [])),
            content=dict(data.get("content", {})),
            content_hash=str(data.get("content_hash", "")),
            benchmark=BenchmarkDelta.from_dict(benchmark) if benchmark else None,
            regression=RegressionDelta.from_dict(regression) if regression else None,
            submitted_by=str(data.get("submitted_by", "loop")),
            reviewed_by=data.get("reviewed_by"),
            review_note=data.get("review_note"),
            rejection_reason=data.get("rejection_reason"),
            duplicate_of=data.get("duplicate_of"),
            version=int(data.get("version", 1)),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
        )
