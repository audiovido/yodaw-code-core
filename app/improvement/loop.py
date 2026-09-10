"""Bounded self-improving iteration loop.

One ``run_iteration`` executes the full governed chain exactly once:

BENCHMARK (injected results) -> FAILURE MINING -> ROOT-CAUSE CLUSTER
-> IMPROVEMENT PROPOSAL -> SKILL/POLICY CANDIDATE -> SANDBOX
VALIDATION -> REGRESSION CHECK -> APPROVAL GATE (submission only;
actual approval stays human) -> MONITOR (audit) -> ROLLBACK (on
demand, never automatic).

``run`` repeats ``run_iteration`` up to ``max_iterations`` and stops
early when an iteration produces no new clusters, no new proposals,
or a validation/regression failure — the loop is bounded and never
runs open-ended. Activation is never performed by the loop; it
requires the explicit approval gate plus a versioned activation call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.eval.models import EvaluationResult
from app.improvement.approval import submit_for_review
from app.improvement.audit import LoopAudit
from app.improvement.candidates import generate_candidates
from app.improvement.models import ProposalStatus
from app.improvement.store import ProposalStore
from app.improvement.validation import (
    RegressionScores,
    SuiteScores,
    check_regression,
    validate_proposal,
)
from app.improvement.versioning import ActivationStore
from app.learning.clusters import cluster_signals
from app.learning.failure_miner import mine_failures
from app.storage.db import DB_PATH

DEFAULT_MAX_ITERATIONS = 5
DEFAULT_MIN_OCCURRENCES = 3


@dataclass
class IterationReport:
    iteration: int
    signals: int
    quarantined: int
    clusters: int
    ignored: int
    proposals_created: int
    duplicates_suppressed: int
    unsafe_rejected: int
    validated: int
    validation_failures: int
    regression_failures: int
    submitted: int
    stopped: bool = False
    stop_reason: str | None = None
    audit_events: int = 0


@dataclass
class LoopReport:
    iterations: list[IterationReport] = field(default_factory=list)
    total_proposals: int = 0
    total_activated: int = 0

    @property
    def ran_iterations(self) -> int:
        return len(self.iterations)


class SelfImprovingLoop:
    """Governed, bounded loop over injected benchmark results."""

    def __init__(
        self,
        path: Path | str = DB_PATH,
        *,
        proposals: ProposalStore | None = None,
        activations: ActivationStore | None = None,
        audit: LoopAudit | None = None,
        registry=None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
    ):
        self.path = Path(path)
        self.proposals = proposals or ProposalStore(path=self.path)
        self.activations = activations or ActivationStore(
            path=self.path, proposals=self.proposals
        )
        self.audit = audit or LoopAudit(path=self.path)
        self.registry = registry
        self.max_iterations = max(1, int(max_iterations))
        self.min_occurrences = max(2, int(min_occurrences))

    # --------------------------------------------------
    # Single bounded iteration
    # --------------------------------------------------

    def run_iteration(
        self,
        results: list[EvaluationResult],
        *,
        iteration: int = 1,
        suite_scores: dict[str, SuiteScores] | None = None,
        regression_scores: dict[str, RegressionScores] | None = None,
    ) -> IterationReport:
        suite_scores = suite_scores or {}
        regression_scores = regression_scores or {}
        report = IterationReport(
            iteration=iteration,
            signals=0,
            quarantined=0,
            clusters=0,
            ignored=0,
            proposals_created=0,
            duplicates_suppressed=0,
            unsafe_rejected=0,
            validated=0,
            validation_failures=0,
            regression_failures=0,
            submitted=0,
        )

        signals, quarantined = mine_failures(results)
        report.signals = len(signals)
        report.quarantined = len(quarantined)
        self.audit.record(
            "loop.mined",
            {
                "iteration": iteration,
                "signals": len(signals),
                "quarantined": len(quarantined),
            },
        )

        clusters, ignored = cluster_signals(
            signals, min_occurrences=self.min_occurrences
        )
        report.clusters = len(clusters)
        report.ignored = len(ignored)
        self.audit.record(
            "loop.clustered",
            {
                "iteration": iteration,
                "clusters": [c.id for c in clusters],
                "ignored": len(ignored),
            },
        )

        if not clusters:
            report.stopped = True
            report.stop_reason = "no recurring clusters"
            return report

        for cluster in clusters:
            candidates, rejections = generate_candidates(
                cluster, registry=self.registry
            )
            if not candidates:
                report.unsafe_rejected += 1
                self.audit.record(
                    "loop.proposal_rejected_unsafe",
                    {"cluster_id": cluster.id, "reasons": rejections},
                )
                continue

            for candidate in candidates:
                proposal, created = self.proposals.create(
                    kind=candidate.kind,
                    title=candidate.title,
                    description=candidate.description,
                    cluster_id=cluster.id,
                    expected_impact=candidate.expected_impact,
                    risk_score=candidate.risk_score,
                    validation_plan=candidate.validation_plan,
                    content=candidate.content,
                    content_hash=candidate.content_hash,
                )
                if not created:
                    report.duplicates_suppressed += 1
                    self.audit.record(
                        "loop.proposal_duplicate_suppressed",
                        {
                            "cluster_id": cluster.id,
                            "duplicate_of": proposal.id,
                        },
                    )
                    continue

                report.proposals_created += 1
                self.audit.record(
                    "loop.proposal_created",
                    {"proposal_id": proposal.id, "cluster_id": cluster.id},
                )

                if proposal.id in suite_scores:
                    outcome = validate_proposal(
                        proposal, suite_scores[proposal.id]
                    )
                    self.proposals.save(proposal)
                    self.audit.record(
                        "loop.validated",
                        {
                            "proposal_id": proposal.id,
                            "passed": outcome.passed,
                            "reasons": outcome.reasons,
                        },
                    )
                    if not outcome.passed:
                        report.validation_failures += 1
                        continue
                    report.validated += 1

                if proposal.id in regression_scores:
                    outcome = check_regression(
                        proposal, regression_scores[proposal.id]
                    )
                    self.proposals.save(proposal)
                    self.audit.record(
                        "loop.regression_checked",
                        {
                            "proposal_id": proposal.id,
                            "passed": outcome.passed,
                            "reasons": outcome.reasons,
                        },
                    )
                    if not outcome.passed:
                        report.regression_failures += 1
                        continue

                if proposal.status == ProposalStatus.DRAFT:
                    submit_for_review(self.proposals, proposal.id)
                    report.submitted += 1
                    self.audit.record(
                        "loop.submitted", {"proposal_id": proposal.id}
                    )

        if report.proposals_created == 0:
            report.stopped = True
            report.stop_reason = "no new proposals (duplicates or unsafe)"
        elif report.validation_failures or report.regression_failures:
            report.stopped = True
            report.stop_reason = "validation or regression failure"

        report.audit_events = len(self.audit.events())
        return report

    # --------------------------------------------------
    # Bounded multi-iteration run
    # --------------------------------------------------

    def run(
        self,
        batches: list[list[EvaluationResult]],
        *,
        suite_scores: dict[str, SuiteScores] | None = None,
        regression_scores: dict[str, RegressionScores] | None = None,
    ) -> LoopReport:
        """Run up to ``max_iterations`` batches, stopping early."""
        report = LoopReport()
        batches = list(batches)[: self.max_iterations]
        for index, results in enumerate(batches, start=1):
            iteration = self.run_iteration(
                results,
                iteration=index,
                suite_scores=suite_scores,
                regression_scores=regression_scores,
            )
            report.iterations.append(iteration)
            self.audit.record(
                "loop.run",
                {
                    "iteration": index,
                    "proposals_created": iteration.proposals_created,
                    "stopped": iteration.stopped,
                    "stop_reason": iteration.stop_reason,
                },
            )
            if iteration.stopped:
                break
        report.total_proposals = self.proposals.count()
        current = self.activations.current()
        report.total_activated = 1 if current is not None else 0
        return report
