"""Sandbox validation and regression checking for proposals.

Validation replays the proposal's clustered benchmark suite in a
sandbox (no network, no live services — the suite runs are injected
by the caller) and refuses to pass when the after-score does not beat
the before-score. The regression check fails the proposal on any new
failure (``failures_after > failures_before``) or when the check
itself did not pass. Both outcomes are recorded on the proposal as
``BenchmarkDelta`` / ``RegressionDelta`` evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.improvement.models import (
    BenchmarkDelta,
    ImprovementProposal,
    RegressionDelta,
)


@dataclass
class SuiteScores:
    suite: str
    before_score: float
    after_score: float
    cases_run: int = 0


@dataclass
class RegressionScores:
    suite: str
    failures_before: int
    failures_after: int
    passed: bool = False


@dataclass
class ValidationOutcome:
    passed: bool
    reasons: list[str]


def validate_proposal(
    proposal: ImprovementProposal, scores: SuiteScores
) -> ValidationOutcome:
    """Attach benchmark before/after evidence and judge improvement."""
    reasons: list[str] = []
    delta = BenchmarkDelta(
        suite=scores.suite,
        before_score=float(scores.before_score),
        after_score=float(scores.after_score),
        cases_run=int(scores.cases_run),
    )
    proposal.benchmark = delta

    if scores.cases_run <= 0:
        reasons.append("no validation cases were run")
    if delta.improvement <= 0:
        reasons.append(
            f"no improvement: before={delta.before_score} "
            f"after={delta.after_score}"
        )
    if not proposal.validation_plan:
        reasons.append("proposal has no validation plan")

    passed = not reasons
    if passed:
        reasons.append(
            f"improved by {delta.improvement:.2f} on {scores.suite}"
        )
    return ValidationOutcome(passed=passed, reasons=reasons)


def check_regression(
    proposal: ImprovementProposal, scores: RegressionScores
) -> ValidationOutcome:
    """Attach regression evidence and reject on any degradation."""
    reasons: list[str] = []
    delta = RegressionDelta(
        suite=scores.suite,
        failures_before=int(scores.failures_before),
        failures_after=int(scores.failures_after),
        passed=bool(scores.passed),
    )
    proposal.regression = delta

    if not delta.passed:
        reasons.append(f"regression suite {scores.suite} did not pass")
    if delta.delta > 0:
        reasons.append(
            f"regression degradation: {delta.failures_before} -> "
            f"{delta.failures_after} failures"
        )

    passed = not reasons
    if passed:
        reasons.append(f"no regressions on {scores.suite}")
    return ValidationOutcome(passed=passed, reasons=reasons)
