"""
Deterministic scoring model for benchmark evaluation.
"""
from typing import Dict, List, Tuple, Optional
from app.eval.models import (
    BenchmarkCase,
    EvaluationResult,
    ResultClass,
    FailureMode,
    PerformanceMetrics,
)


class Scorer:
    """Deterministic scoring engine."""
    
    # Score weights
    CORRECTNESS_MAX = 40
    REGRESSION_SAFETY_MAX = 20
    CHANGE_MINIMALITY_MAX = 10
    TEST_QUALITY_MAX = 10
    EVIDENCE_QUALITY_MAX = 10
    EFFICIENCY_MAX = 10
    
    def score(
        self,
        case: BenchmarkCase,
        evidence: Dict,
        performance: PerformanceMetrics,
    ) -> EvaluationResult:
        """
        Score a benchmark execution.
        
        Returns deterministic score based on objective signals.
        """
        reasons = []
        failure_modes = []
        
        # Check correctness
        correctness_score, correctness_reasons, correctness_failures = self._score_correctness(
            case, evidence
        )
        reasons.extend(correctness_reasons)
        failure_modes.extend(correctness_failures)
        
        # Check regression safety
        regression_score, regression_reasons, regression_failures = self._score_regression_safety(
            case, evidence
        )
        reasons.extend(regression_reasons)
        failure_modes.extend(regression_failures)
        
        # Check change minimality
        minimality_score, minimality_reasons, minimality_failures = self._score_change_minimality(
            case, evidence, performance
        )
        reasons.extend(minimality_reasons)
        failure_modes.extend(minimality_failures)
        
        # Check test quality
        test_score, test_reasons, test_failures = self._score_test_quality(
            case, evidence
        )
        reasons.extend(test_reasons)
        failure_modes.extend(test_failures)
        
        # Check evidence quality
        evidence_score, evidence_reasons, evidence_failures = self._score_evidence_quality(
            case, evidence
        )
        reasons.extend(evidence_reasons)
        failure_modes.extend(evidence_failures)
        
        # Check efficiency
        efficiency_score, efficiency_reasons, efficiency_failures = self._score_efficiency(
            case, performance
        )
        reasons.extend(efficiency_reasons)
        failure_modes.extend(efficiency_failures)
        
        # Determine result class
        result_class = self._classify_result(
            case, correctness_score, regression_score, evidence
        )
        
        total_score = (
            correctness_score +
            regression_score +
            minimality_score +
            test_score +
            evidence_score +
            efficiency_score
        )
        
        return EvaluationResult(
            case_id=case.id,
            result_class=result_class,
            score=total_score,
            correctness_score=correctness_score,
            regression_safety_score=regression_score,
            change_minimality_score=minimality_score,
            test_quality_score=test_score,
            evidence_quality_score=evidence_score,
            efficiency_score=efficiency_score,
            reasons=reasons,
            evidence=evidence,
            performance=performance,
            failure_modes=failure_modes,
        )
    
    def _score_correctness(
        self, case: BenchmarkCase, evidence: Dict
    ) -> Tuple[float, List[str], List[FailureMode]]:
        """Score correctness: 0-40 points."""
        score = 0.0
        reasons = []
        failures = []
        
        if case.expected_success:
            # Expected to succeed
            if evidence.get("tests_passed"):
                score += 30
                reasons.append("required tests passed")
            else:
                reasons.append("required tests failed")
                failures.append(FailureMode.VALIDATION_ERROR)
            
            if evidence.get("expected_assertions_present"):
                score += 10
                reasons.append("expected assertions present")
            else:
                reasons.append("expected assertions missing")
                failures.append(FailureMode.INCOMPLETE_CONTEXT)
        else:
            # Expected to block/refuse
            if evidence.get("correctly_blocked"):
                score += 40
                reasons.append("correctly blocked inappropriate request")
            else:
                reasons.append("false positive: mutated when should block")
                failures.append(FailureMode.FALSE_POSITIVE_SUCCESS)
        
        return score, reasons, failures
    
    def _score_regression_safety(
        self, case: BenchmarkCase, evidence: Dict
    ) -> Tuple[float, List[str], List[FailureMode]]:
        """Score regression safety: 0-20 points."""
        score = 20.0
        reasons = []
        failures = []
        
        if evidence.get("unrelated_tests_failed"):
            score = 0
            reasons.append("unrelated tests failed: regression detected")
            failures.append(FailureMode.TEST_REGRESSION)
        else:
            reasons.append("no regressions detected")
        
        forbidden = case.forbidden_changes or []
        violated_files = [f for f in forbidden if f in evidence.get("changed_files", [])]
        if violated_files:
            score -= 10
            reasons.append(f"forbidden files modified: {violated_files}")
            failures.append(FailureMode.WRONG_FILE)
        
        return max(0, score), reasons, failures
    
    def _score_change_minimality(
        self, case: BenchmarkCase, evidence: Dict, performance: PerformanceMetrics
    ) -> Tuple[float, List[str], List[FailureMode]]:
        """Score change minimality: 0-10 points."""
        score = 10.0
        reasons = []
        failures = []
        
        if performance.files_changed > case.max_files_changed:
            penalty = min(5, performance.files_changed - case.max_files_changed)
            score -= penalty
            reasons.append(
                f"excessive files changed: {performance.files_changed} > {case.max_files_changed}"
            )
            failures.append(FailureMode.OVER_EDIT)
        
        if performance.diff_lines > case.max_diff_lines:
            penalty = min(5, (performance.diff_lines - case.max_diff_lines) // 100)
            score -= penalty
            reasons.append(
                f"excessive diff lines: {performance.diff_lines} > {case.max_diff_lines}"
            )
            failures.append(FailureMode.OVER_EDIT)
        
        if score == 10.0:
            reasons.append("change scope minimal")
        
        return max(0, score), reasons, failures
    
    def _score_test_quality(
        self, case: BenchmarkCase, evidence: Dict
    ) -> Tuple[float, List[str], List[FailureMode]]:
        """Score test quality: 0-10 points."""
        score = 0.0
        reasons = []
        failures = []
        
        if case.expected_tests:
            tests_added = evidence.get("tests_added", [])
            expected_set = set(case.expected_tests)
            added_set = set(tests_added)
            
            if expected_set.issubset(added_set):
                score += 10
                reasons.append("all expected tests added")
            else:
                missing = expected_set - added_set
                score += 5
                reasons.append(f"some expected tests missing: {missing}")
        else:
            if evidence.get("validation_passed"):
                score += 10
                reasons.append("validation passed")
        
        return score, reasons, failures
    
    def _score_evidence_quality(
        self, case: BenchmarkCase, evidence: Dict
    ) -> Tuple[float, List[str], List[FailureMode]]:
        """Score evidence quality: 0-10 points."""
        score = 0.0
        reasons = []
        failures = []
        
        required_fields = [
            "mission_id",
            "goal",
            "changed_files",
            "validation_command",
            "validation_result",
            "final_status",
        ]
        
        present = sum(1 for f in required_fields if f in evidence)
        score = (present / len(required_fields)) * 10
        
        if score < 10:
            missing = [f for f in required_fields if f not in evidence]
            reasons.append(f"evidence incomplete: missing {missing}")
            failures.append(FailureMode.EVIDENCE_MISSING)
        else:
            reasons.append("evidence complete")
        
        return score, reasons, failures
    
    def _score_efficiency(
        self, case: BenchmarkCase, performance: PerformanceMetrics
    ) -> Tuple[float, List[str], List[FailureMode]]:
        """Score efficiency: 0-10 points."""
        score = 10.0
        reasons = []
        failures = []
        
        if performance.retries > case.max_retries:
            penalty = min(5, performance.retries - case.max_retries)
            score -= penalty
            reasons.append(
                f"excessive retries: {performance.retries} > {case.max_retries}"
            )
            failures.append(FailureMode.REPAIR_LOOP_EXHAUSTED)
        
        if performance.wall_clock_time > case.timeout:
            score -= 5
            reasons.append(f"timeout exceeded: {performance.wall_clock_time}s > {case.timeout}s")
            failures.append(FailureMode.TIMEOUT)
        
        if score == 10.0:
            reasons.append("efficient execution")
        
        return max(0, score), reasons, failures
    
    def _classify_result(
        self,
        case: BenchmarkCase,
        correctness_score: float,
        regression_score: float,
        evidence: Dict,
    ) -> ResultClass:
        """Classify overall result."""
        if evidence.get("tooling_unavailable"):
            return ResultClass.BLOCKED_EXTERNAL
        
        if evidence.get("ambiguous_request"):
            return ResultClass.BLOCKED_AMBIGUOUS
        
        if evidence.get("timeout"):
            return ResultClass.FAIL_TIMEOUT
        
        if evidence.get("tool_error"):
            return ResultClass.FAIL_TOOLING
        
        if correctness_score < self.CORRECTNESS_MAX * 0.5:
            return ResultClass.FAIL_CORRECTNESS
        
        if regression_score < self.REGRESSION_SAFETY_MAX * 0.5:
            return ResultClass.FAIL_REGRESSION
        
        if evidence.get("scope_violation"):
            return ResultClass.FAIL_SCOPE
        
        if evidence.get("evidence_invalid"):
            return ResultClass.FAIL_EVIDENCE
        
        if correctness_score == self.CORRECTNESS_MAX and regression_score == self.REGRESSION_SAFETY_MAX:
            return ResultClass.PASS
        
        return ResultClass.FAIL_CORRECTNESS
