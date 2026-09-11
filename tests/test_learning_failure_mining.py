"""Hermetic tests for failure mining and root-cause clustering."""
import pytest
from typing import Optional

from app.eval.models import (
    EvaluationResult,
    FailureMode,
    PerformanceMetrics,
    ResultClass,
)
from app.learning.clusters import cluster_signals
from app.learning.failure_miner import mine_failures, sanitize_results


def _perf() -> PerformanceMetrics:
    return PerformanceMetrics(
        wall_clock_time=1.0,
        retries=0,
        files_changed=1,
        diff_lines=10,
    )


def _result(
    case_id: str,
    kind: ResultClass,
    modes: Optional[list[FailureMode]] = None,
    score: float = 50.0,
    evidence: Optional[dict] = None,
) -> EvaluationResult:
    return EvaluationResult(
        case_id=case_id,
        result_class=kind,
        score=score,
        correctness_score=10.0,
        regression_safety_score=10.0,
        change_minimality_score=5.0,
        test_quality_score=5.0,
        evidence_quality_score=5.0,
        efficiency_score=5.0,
        reasons=["test"],
        evidence=evidence or {"mission_id": case_id},
        performance=_perf(),
        failure_modes=modes or [],
    )


def _fail(case_id: str) -> EvaluationResult:
    return _result(
        case_id,
        ResultClass.FAIL_CORRECTNESS,
        [FailureMode.VALIDATION_ERROR],
    )


def test_recurring_failure_cluster():
    results = [_fail(f"case-{i}") for i in range(4)]
    signals, quarantined = mine_failures(results)
    assert not quarantined
    clusters, ignored = cluster_signals(signals, min_occurrences=3)
    assert len(clusters) == 1
    assert clusters[0].size == 4
    assert not ignored


def test_unique_failure_ignored():
    results = [_fail("only-one")]
    signals, _ = mine_failures(results)
    clusters, ignored = cluster_signals(signals, min_occurrences=3)
    assert clusters == []
    assert len(ignored) == 1


def test_pass_and_blocked_yield_no_signals():
    results = [
        _result("pass-1", ResultClass.PASS),
        _result("blocked-1", ResultClass.BLOCKED_EXTERNAL),
    ]
    signals, quarantined = mine_failures(results)
    assert signals == []
    assert quarantined == []


def test_poisoned_benchmark_quarantined():
    poisoned = _result(
        "case-poisoned",
        ResultClass.FAIL_CORRECTNESS,
        [FailureMode.VALIDATION_ERROR],
        evidence={"override_score": True},
    )
    out_of_bounds = _result(
        "case-bad-score",
        ResultClass.FAIL_CORRECTNESS,
        [FailureMode.VALIDATION_ERROR],
        score=999.0,
    )
    signals, quarantined = mine_failures([poisoned, out_of_bounds])
    assert signals == []
    assert len(quarantined) == 2
    clean, quarantined2 = sanitize_results([poisoned])
    assert clean == []
    assert quarantined2[0].case_id == "case-poisoned"
