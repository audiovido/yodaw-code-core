"""Explicit approval gate for improvement proposals.

No proposal activates without passing through this gate: submission
moves DRAFT -> PENDING_REVIEW, and only an explicit ``approve`` call
with a named reviewer moves PENDING_REVIEW -> APPROVED. ``reject``
retains the proposal with its reason. Every decision requires a
non-empty reviewer identity so approvals are attributable.
"""

from __future__ import annotations

from app.improvement.models import ImprovementProposal, ProposalStatus
from app.improvement.store import ProposalStore


def _require_reviewer(reviewer: str) -> str:
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ValueError("a named reviewer is required")
    return reviewer


def submit_for_review(
    store: ProposalStore, proposal_id: str, *, actor: str = "loop"
) -> ImprovementProposal:
    """Move DRAFT -> PENDING_REVIEW."""
    return store.transition(
        proposal_id, ProposalStatus.PENDING_REVIEW, actor=actor
    )


def approve(
    store: ProposalStore,
    proposal_id: str,
    *,
    reviewer: str,
    note: str | None = None,
) -> ImprovementProposal:
    """Move PENDING_REVIEW -> APPROVED with an attributable reviewer."""
    return store.transition(
        proposal_id,
        ProposalStatus.APPROVED,
        actor=_require_reviewer(reviewer),
        note=note,
    )


def reject(
    store: ProposalStore,
    proposal_id: str,
    *,
    reviewer: str,
    reason: str,
) -> ImprovementProposal:
    """Move PENDING_REVIEW -> REJECTED, retaining the reason."""
    reviewer = _require_reviewer(reviewer)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a rejection reason is required")
    return store.transition(
        proposal_id,
        ProposalStatus.REJECTED,
        actor=reviewer,
        note=reason,
        reason=reason,
    )
