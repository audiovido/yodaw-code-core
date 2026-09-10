"""
Benchmark runner and orchestration.
"""
import time
import os
import shutil
import subprocess
import tempfile
import json
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime

from app.eval.models import (
    BenchmarkCase,
    PerformanceMetrics,
    EvaluationResult,
    BenchmarkReport,
    ResultClass,
)
from app.eval.fixtures import FixtureManager
from app.eval.scoring import Scorer
from app.eval.evidence import EvidenceValidator


class HermeticProvider:
    """Hermetic fake provider for deterministic testing."""
    
    def execute_mission(self, goal: str, repo_path: Path) -> Dict:
        """
        Fake execution that returns deterministic evidence.
        
        In real evaluation, this would call the actual YODAW Coder.
        """
        return {
            "mission_id": "hermetic_fake_001",
            "goal": goal,
            "changed_files": [],
            "diff": "",
            "validation_command": "echo 'hermetic mode'",
            "validation_result": "passed",
            "final_status": "success",
            "tests_passed": True,
            "hermetic": True,
        }


class BenchmarkRunner:
    """Execute and score benchmark cases."""
    
    def __init__(
        self,
        fixtures_manager: Optional[FixtureManager] = None,
        scorer: Optional[Scorer] = None,
        evidence_validator: Optional[EvidenceValidator] = None,
        hermetic: bool = True,
    ):
        self.fixtures_manager = fixtures_manager or FixtureManager()
        self.scorer = scorer or Scorer()
        self.evidence_validator = evidence_validator or EvidenceValidator()
        self.hermetic = hermetic
        self.provider = HermeticProvider() if hermetic else None
    
    def run_benchmark(
        self,
        case: BenchmarkCase,
        preserve_on_failure: bool = False,
    ) -> EvaluationResult:
        """
        Execute a single benchmark case.
        """
        start_time = time.time()
        repo_path = None
        
        try:
            # Create temporary repository
            repo_path = self.fixtures_manager.create_temp_repo(
                case.repository_fixture
            )
            
            # Execute mission (hermetic or real)
            if self.hermetic:
                evidence = self.provider.execute_mission(case.user_goal, repo_path)
            else:
                evidence = self._execute_real_mission(case, repo_path)
            
            # Collect performance metrics
            wall_clock_time = time.time() - start_time
            performance = PerformanceMetrics(
                wall_clock_time=wall_clock_time,
                retries=evidence.get("retries", 0),
                files_changed=len(evidence.get("changed_files", [])),
                diff_lines=self._count_diff_lines(evidence.get("diff", "")),
                commits_created=1 if evidence.get("commit_sha") else 0,
            )
            
            # Validate evidence
            valid, validation_reasons = self.evidence_validator.validate(case, evidence)
            if not valid:
                evidence["evidence_invalid"] = True
                evidence["validation_reasons"] = validation_reasons
            
            # Score
            result = self.scorer.score(case, evidence, performance)
            
            return result
            
        except Exception as e:
            # Handle execution errors
            return EvaluationResult(
                case_id=case.id,
                result_class=ResultClass.FAIL_TOOLING,
                score=0.0,
                correctness_score=0.0,
                regression_safety_score=0.0,
                change_minimality_score=0.0,
                test_quality_score=0.0,
                evidence_quality_score=0.0,
                efficiency_score=0.0,
                reasons=[f"execution error: {str(e)}"],
                evidence={"error": str(e)},
                performance=PerformanceMetrics(
                    wall_clock_time=time.time() - start_time,
                    retries=0,
                    files_changed=0,
                    diff_lines=0,
                ),
            )
        finally:
            # Cleanup
            if repo_path and not preserve_on_failure:
                try:
                    self.fixtures_manager.cleanup_temp_repo(repo_path)
                except Exception:
                    pass
    
    def run_suite(
        self,
        cases: List[BenchmarkCase],
        case_filter: Optional[str] = None,
        tag_filter: Optional[str] = None,
    ) -> BenchmarkReport:
        """
        Run a suite of benchmark cases.
        """
        # Filter cases
        filtered_cases = cases
        if case_filter:
            filtered_cases = [c for c in filtered_cases if c.id == case_filter]
        if tag_filter:
            filtered_cases = [c for c in filtered_cases if tag_filter in c.tags]
        
        # Execute all cases
        results = []
        for case in filtered_cases:
            result = self.run_benchmark(case)
            results.append(result)
        
        # Aggregate statistics
        total = len(results)
        passed = sum(1 for r in results if r.result_class == ResultClass.PASS)
        blocked = sum(1 for r in results if "BLOCKED" in r.result_class.value)
        failed = total - passed - blocked
        
        overall_score = sum(r.score for r in results) / total if total > 0 else 0.0
        
        failures_by_class = {}
        for r in results:
            cls = r.result_class.value
            failures_by_class[cls] = failures_by_class.get(cls, 0) + 1
        
        failures_by_mode = {}
        for r in results:
            for mode in r.failure_modes:
                mode_str = mode.value
                failures_by_mode[mode_str] = failures_by_mode.get(mode_str, 0) + 1
        
        avg_runtime = sum(r.performance.wall_clock_time for r in results) / total if total > 0 else 0.0
        avg_retries = sum(r.performance.retries for r in results) / total if total > 0 else 0.0
        avg_files = sum(r.performance.files_changed for r in results) / total if total > 0 else 0.0
        
        # Get git revision
        revision = self._get_git_revision()
        
        return BenchmarkReport(
            revision=revision,
            timestamp=datetime.now().isoformat(),
            total_cases=total,
            passed=passed,
            failed=failed,
            blocked=blocked,
            overall_score=overall_score,
            results=results,
            failures_by_class=failures_by_class,
            failures_by_mode=failures_by_mode,
            average_runtime=avg_runtime,
            average_retries=avg_retries,
            average_files_changed=avg_files,
        )
    
    def _execute_real_mission(self, case: BenchmarkCase, repo_path: Path) -> Dict:
        """
        Execute mission using real YODAW Coder.
        
        This is a placeholder for live evaluation mode.
        """
        raise NotImplementedError("Live evaluation mode not yet implemented")
    
    def _count_diff_lines(self, diff: str) -> int:
        """Count changed lines in diff."""
        if not diff:
            return 0
        lines = diff.split("\n")
        return sum(1 for line in lines if line.startswith("+") or line.startswith("-"))
    
    def _get_git_revision(self) -> str:
        """Get current git revision."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=os.path.dirname(__file__),
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return "unknown"


class ComparisonRunner:
    """Compare two YODAW revisions using isolated git worktrees."""

    def __init__(self, runner: BenchmarkRunner, repo_root=None):
        self.runner = runner
        self.repo_root = Path(repo_root) if repo_root else self._find_repo_root()

    @staticmethod
    def _find_repo_root() -> Path:
        here = Path(__file__).resolve()
        for parent in [here] + list(here.parents):
            if (parent / ".git").exists():
                return parent
        return here.parents[3]

    def _run_git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=str(cwd or self.repo_root),
        )

    def _resolve_sha(self, sha: str) -> str:
        result = self._run_git("rev-parse", "--verify", sha)
        if result.returncode != 0:
            raise ValueError(f"Unknown revision: {sha}")
        return result.stdout.strip()

    def _make_worktree(self, sha: str) -> Path:
        resolved = self._resolve_sha(sha)
        work_dir = Path(tempfile.mkdtemp(prefix="yodaw_eval_cmp_"))
        result = self._run_git("worktree", "add", "--detach", str(work_dir), resolved)
        if result.returncode != 0:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise RuntimeError(f"git worktree add failed for {sha}: {result.stderr.strip()}")
        return work_dir

    def _remove_worktree(self, work_dir: Path) -> None:
        self._run_git("worktree", "remove", "--force", str(work_dir))
        shutil.rmtree(work_dir, ignore_errors=True)

    def _run_suite_in(self, work_dir: Path, cases: List[BenchmarkCase]) -> BenchmarkReport:
        revision = self._run_git("rev-parse", "HEAD", cwd=work_dir)
        report = self.runner.run_suite(cases)
        report.revision = revision.stdout.strip() if revision.returncode == 0 else "unknown"
        return report

    def _summarize(self, report: BenchmarkReport) -> Dict:
        return {
            "revision": report.revision,
            "total_cases": report.total_cases,
            "passed": report.passed,
            "failed": report.failed,
            "blocked": report.blocked,
            "overall_score": report.overall_score,
            "passed_ids": sorted(r.case_id for r in report.results if r.result_class == ResultClass.PASS),
        }

    def compare_revisions(
        self,
        base_sha: str,
        candidate_sha: str,
        cases: List[BenchmarkCase],
    ) -> Dict:
        """
        Compare two revisions on benchmark suite.

        Each revision is checked out in an isolated git worktree so the
        comparison never mutates the caller's working tree.

        Returns comparison report.
        """
        base_dir = None
        candidate_dir = None
        try:
            base_dir = self._make_worktree(base_sha)
            candidate_dir = self._make_worktree(candidate_sha)
            base_report = self._run_suite_in(base_dir, cases)
            candidate_report = self._run_suite_in(candidate_dir, cases)
        finally:
            if base_dir is not None:
                self._remove_worktree(base_dir)
            if candidate_dir is not None:
                self._remove_worktree(candidate_dir)

        base_passed = {r.case_id for r in base_report.results if r.result_class == ResultClass.PASS}
        candidate_passed = {r.case_id for r in candidate_report.results if r.result_class == ResultClass.PASS}
        not_blocked = (ResultClass.PASS, ResultClass.BLOCKED_EXTERNAL, ResultClass.BLOCKED_AMBIGUOUS)
        base_failed = {r.case_id for r in base_report.results if r.result_class not in not_blocked}
        candidate_failed = {r.case_id for r in candidate_report.results if r.result_class not in (ResultClass.PASS, ResultClass.BLOCKED_EXTERNAL, ResultClass.BLOCKED_AMBIGUOUS)}

        return {
            "base_sha": base_report.revision,
            "candidate_sha": candidate_report.revision,
            "base": self._summarize(base_report),
            "candidate": self._summarize(candidate_report),
            "score_delta": candidate_report.overall_score - base_report.overall_score,
            "newly_passed": sorted(candidate_passed - base_passed),
            "newly_failed": sorted(base_passed - candidate_passed),
            "unchanged_failures": sorted(base_failed & candidate_failed),
            "performance_delta": {
                "base_avg_runtime": base_report.average_runtime,
                "candidate_avg_runtime": candidate_report.average_runtime,
                "runtime_delta": candidate_report.average_runtime - base_report.average_runtime,
            },
        }


def print_report_summary(report: BenchmarkReport):
    """Print human-readable report summary."""
    print("\n" + "=" * 60)
    print("YODAW CODER EVALUATION REPORT")
    print("=" * 60)
    print(f"Revision: {report.revision}")
    print(f"Timestamp: {report.timestamp}")
    print()
    print(f"Total Cases: {report.total_cases}")
    print(f"Passed: {report.passed}")
    print(f"Failed: {report.failed}")
    print(f"Blocked: {report.blocked}")
    print(f"Overall Score: {report.overall_score:.2f}/100")
    print()
    print("Performance:")
    print(f"  Average Runtime: {report.average_runtime:.2f}s")
    print(f"  Average Retries: {report.average_retries:.2f}")
    print(f"  Average Files Changed: {report.average_files_changed:.2f}")
    print()
    print("Failures by Class:")
    for cls, count in sorted(report.failures_by_class.items()):
        print(f"  {cls}: {count}")
    print()
    if report.failures_by_mode:
        print("Failures by Mode:")
        for mode, count in sorted(report.failures_by_mode.items()):
            print(f"  {mode}: {count}")
    print("=" * 60)
