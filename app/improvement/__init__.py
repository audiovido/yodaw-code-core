"""Governed self-improving loop for YODAW.

Benchmark -> failure mining -> root-cause clustering -> improvement
proposal -> skill/policy candidate -> sandbox validation -> regression
check -> approval gate -> versioned activation -> monitor -> rollback.

No silent self-modification: every state change is an explicit,
audited transition, and activation requires policy approval.
"""

from app.improvement.models import (
    BenchmarkDelta,
    ImprovementProposal,
    ProposalKind,
    ProposalStatus,
    RegressionDelta,
)

__all__ = [
    "BenchmarkDelta",
    "ImprovementProposal",
    "ProposalKind",
    "ProposalStatus",
    "RegressionDelta",
]
