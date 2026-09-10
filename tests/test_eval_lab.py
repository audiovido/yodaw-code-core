"""Hermetic tests for the YODAW evaluation lab (Worker D)."""
import json

import pytest

from app.eval import benchmarks as bench_mod
from app.eval.benchmarks import ALL_BENCHMARKS, get_benchmark_by_id, get_benchmarks_by_tag
from app.eval.evidence import EvidenceValidator
from app.eval.fixtures import FixtureManager
from app.eval.models import (
    BenchmarkCase,
    EvaluationResult,
    Intent,
    PerformanceMetrics,
    ResultClass,
)
from app.eval.runner import BenchmarkRunner, ComparisonRunner
from app.eval.scoring import Scorer


def _perf(**kwargs):
    base = {"wall_clock_time": 1.0, "retries": 0, "files_changed": 1, "diff_lines": 10}
    base.update(kwargs)
    return PerformanceMetrics(**base)


def _success_evidence(**kwargs):
    evidence = {
        "mission_id": "m1",
        "goal": "do thing",
        "changed_files": ["calculator.py"],
        "diff": "+x",
        "validation_command": "pytest",
        "validation_result": "passed",
        "final_status": "success",
        "tests_passed": True,
        "expected_assertions_present": True,
        "tests_added": [],
        "validation_passed": True,
    }
    evidence.update(kwargs)
    return evidence


def test_benchmark_suite_has_expected_size():
    assert len(ALL_BENCHMARKS) == 19
    ids = [b.id for b in ALL_BENCHMARKS]
    assert len(set(ids)) == len(ids)


def test_get_benchmark_by_id():
    case = get_benchmark_by_id("bugfix_basic")
    assert case.intent == Intent.BUGFIX
    with pytest.raises(ValueError):
        get_benchmark_by_id("no_such_case")


def test_get_benchmarks_by_tag():
    python_cases = get_benchmarks_by_tag("python")
    assert python_cases
    assert all("python" in c.tags for c in python_cases)


def test_fixture_manager_creates_and_cleans_repo(tmp_path):
    manager = FixtureManager()
    repo_path = manager.create_temp_repo("python_basic")
    try:
        assert (repo_path / "calculator.py").exists()
        assert (repo_path / "test_calculator.py").exists()
    finally:
        manager.cleanup_temp_repo(repo_path)
    assert not repo_path.parent.exists()


def test_fixture_manager_unknown_fixture():
    with pytest.raises(ValueError):
        FixtureManager().create_temp_repo("no_such_fixture")


@pytest.mark.parametrize("fixture", ["python_basic", "node_basic", "go_basic", "mixed_repo"])
def test_all_text_fixtures_materialize(fixture):
    manager = FixtureManager()
    repo_path = manager.create_temp_repo(fixture)
    try:
        assert any(repo_path.iterdir())
    finally:
        manager.cleanup_temp_repo(repo_path)


def test_scorer_success_gets_full_marks():
    case = get_benchmark_by_id("bugfix_basic")
    evidence = _success_evidence(tests_added=["test_divide_by_zero"])
    result = Scorer().score(case, evidence, _perf())
    assert result.result_class == ResultClass.PASS
    assert result.score == 100.0


def test_scorer_detects_forbidden_change():
    case = get_benchmark_by_id("bugfix_misleading")
    evidence = _success_evidence(changed_files=["calculator.py"])
    result = Scorer().score(case, evidence, _perf())
    assert result.regression_safety_score < Scorer.REGRESSION_SAFETY_MAX


def test_scorer_blocks_impossible_request():
    case = get_benchmark_by_id("negative_impossible")
    evidence = {"blocked_reason": "impossible", "correctly_blocked": True, "changed_files": []}
    result = Scorer().score(case, evidence, _perf(files_changed=0, diff_lines=0))
    assert result.correctness_score == Scorer.CORRECTNESS_MAX


def test_evidence_validator_success_path():
    case = get_benchmark_by_id("bugfix_basic")
    valid, _ = EvidenceValidator().validate(case, _success_evidence())
    assert valid


def test_evidence_validator_rejects_empty_change():
    case = get_benchmark_by_id("bugfix_basic")
    valid, reasons = EvidenceValidator().validate(case, _success_evidence(changed_files=[]))
    assert not valid
    assert reasons


def test_hermetic_runner_runs_single_case():
    runner = BenchmarkRunner(hermetic=True)
    result = runner.run_benchmark(get_benchmark_by_id("bugfix_basic"))
    assert isinstance(result, EvaluationResult)
    assert result.case_id == "bugfix_basic"


def test_hermetic_runner_suite_filters():
    runner = BenchmarkRunner(hermetic=True)
    report = runner.run_suite(ALL_BENCHMARKS, tag_filter="python")
    assert report.total_cases == len(get_benchmarks_by_tag("python"))
    assert report.passed + report.failed + report.blocked == report.total_cases


def test_runner_report_serializes_to_json():
    runner = BenchmarkRunner(hermetic=True)
    report = runner.run_suite([get_benchmark_by_id("bugfix_basic")])
    payload = json.loads(report.to_json())
    assert payload["summary"]["total_cases"] == 1
    assert len(payload["cases"]) == 1


def test_eval_cli_lists_benchmarks():
    from app.eval.__main__ import list_benchmarks
    import argparse

    args = argparse.Namespace(tag=None)
    assert list_benchmarks(args) == 0


def test_comparison_runner_rejects_unknown_sha(tmp_path):
    runner = BenchmarkRunner(hermetic=True)
    comparator = ComparisonRunner(runner, repo_root=tmp_path)
    with pytest.raises(ValueError):
        comparator.compare_revisions("deadbee" * 6, "deadbee" * 6, [])


def test_comparison_runner_uses_isolated_worktrees():
    runner = BenchmarkRunner(hermetic=True)
    comparator = ComparisonRunner(runner)
    comparison = comparator.compare_revisions(
        "HEAD",
        "HEAD",
        [get_benchmark_by_id("bugfix_basic"), get_benchmark_by_id("refactor_rename")],
    )
    assert comparison["base_sha"]
    assert comparison["candidate_sha"] == comparison["base_sha"]
    assert comparison["score_delta"] == pytest.approx(0.0)
    assert comparison["newly_failed"] == []
    assert set(comparison["candidate"]["passed_ids"]) == set(
        comparison["base"]["passed_ids"]
    )
    assert comparison["performance_delta"]["runtime_delta"] == pytest.approx(0.0, abs=30.0)


def test_benchmark_case_round_trip():
    case = get_benchmark_by_id("feature_small")
    restored = BenchmarkCase.from_dict(case.to_dict())
    assert restored == case


def test_run_suite_empty_reports_zero():
    report = BenchmarkRunner(hermetic=True).run_suite([])
    assert report.total_cases == 0
    assert report.overall_score == 0.0
