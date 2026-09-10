"""
Benchmark runner and orchestration.
"""
import time
import os
import subprocess
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
    """Compare two YODAW revisions."""
    
    def __init__(self, runner: BenchmarkRunner):
        self.runner = runner
    
    def compare_revisions(
        self,
        base_sha: str,
        candidate_sha: str,
        cases: List[BenchmarkCase],
    ) -> Dict:
        """
        Compare two revisions on benchmark suite.
        
        Returns comparison report.
        """
        # This would:
        # 1. Checkout base_sha in temp worktree
        # 2. Run benchmarks
        # 3. Checkout candidate_sha in temp worktree
        # 4. Run benchmarks
        # 5. Compare results
        
        # For now, placeholder
        return {
            "base_sha": base_sha,
            "candidate_sha": candidate_sha,
            "score_delta": 0.0,
            "newly_passed": [],
            "newly_failed": [],
            "unchanged_failures": [],
            "performance_delta": {},
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
