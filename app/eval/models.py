"""
Benchmark task format and evaluation result models.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
import json


class Intent(str, Enum):
    """Task intent classification."""
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    FEATURE = "feature"
    TEST = "test"
    REVIEW = "review"
    DEPENDENCY = "dependency"
    MIGRATION = "migration"
    PERFORMANCE = "performance"
    SECURITY = "security"
    MIXED = "mixed"


class ResultClass(str, Enum):
    """Evaluation result classification."""
    PASS = "PASS"
    FAIL_CORRECTNESS = "FAIL_CORRECTNESS"
    FAIL_REGRESSION = "FAIL_REGRESSION"
    FAIL_SCOPE = "FAIL_SCOPE"
    FAIL_EVIDENCE = "FAIL_EVIDENCE"
    FAIL_TIMEOUT = "FAIL_TIMEOUT"
    FAIL_TOOLING = "FAIL_TOOLING"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    BLOCKED_AMBIGUOUS = "BLOCKED_AMBIGUOUS"


class FailureMode(str, Enum):
    """Failure taxonomy."""
    PLANNING_ERROR = "planning_error"
    WRONG_FILE = "wrong_file"
    INCOMPLETE_CONTEXT = "incomplete_context"
    BAD_EDIT = "bad_edit"
    SYNTAX_ERROR = "syntax_error"
    TEST_REGRESSION = "test_regression"
    OVER_EDIT = "over_edit"
    DEPENDENCY_ERROR = "dependency_error"
    TOOLING_MISSING = "tooling_missing"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    VALIDATION_ERROR = "validation_error"
    REPAIR_LOOP_EXHAUSTED = "repair_loop_exhausted"
    COMMIT_GATE_FAILURE = "commit_gate_failure"
    EVIDENCE_MISSING = "evidence_missing"
    FALSE_POSITIVE_SUCCESS = "false_positive_success"
    FALSE_NEGATIVE_FAILURE = "false_negative_failure"


@dataclass
class BenchmarkCase:
    """Structured benchmark case definition."""
    id: str
    title: str
    intent: Intent
    repository_fixture: str
    user_goal: str
    setup: Optional[str] = None
    expected_success: bool = True
    expected_tests: Optional[List[str]] = None
    forbidden_changes: Optional[List[str]] = None
    expected_files: Optional[List[str]] = None
    max_retries: int = 3
    max_files_changed: int = 10
    max_diff_lines: int = 500
    timeout: int = 300
    tags: List[str] = field(default_factory=list)
    difficulty: str = "medium"
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "intent": self.intent.value,
            "repository_fixture": self.repository_fixture,
            "user_goal": self.user_goal,
            "setup": self.setup,
            "expected_success": self.expected_success,
            "expected_tests": self.expected_tests,
            "forbidden_changes": self.forbidden_changes,
            "expected_files": self.expected_files,
            "max_retries": self.max_retries,
            "max_files_changed": self.max_files_changed,
            "max_diff_lines": self.max_diff_lines,
            "timeout": self.timeout,
            "tags": self.tags,
            "difficulty": self.difficulty,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BenchmarkCase":
        """Deserialize from dictionary."""
        data = data.copy()
        data["intent"] = Intent(data["intent"])
        return cls(**data)


@dataclass
class PerformanceMetrics:
    """Performance metrics for a benchmark run."""
    wall_clock_time: float
    retries: int
    files_changed: int
    diff_lines: int
    planning_time: Optional[float] = None
    validation_time: Optional[float] = None
    tool_calls: Optional[int] = None
    commits_created: int = 0


@dataclass
class EvaluationResult:
    """Result of evaluating a single benchmark case."""
    case_id: str
    result_class: ResultClass
    score: float
    correctness_score: float
    regression_safety_score: float
    change_minimality_score: float
    test_quality_score: float
    evidence_quality_score: float
    efficiency_score: float
    reasons: List[str]
    evidence: Dict[str, Any]
    performance: PerformanceMetrics
    failure_modes: List[FailureMode] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "case_id": self.case_id,
            "result_class": self.result_class.value,
            "score": self.score,
            "correctness_score": self.correctness_score,
            "regression_safety_score": self.regression_safety_score,
            "change_minimality_score": self.change_minimality_score,
            "test_quality_score": self.test_quality_score,
            "evidence_quality_score": self.evidence_quality_score,
            "efficiency_score": self.efficiency_score,
            "reasons": self.reasons,
            "evidence": self.evidence,
            "performance": {
                "wall_clock_time": self.performance.wall_clock_time,
                "retries": self.performance.retries,
                "files_changed": self.performance.files_changed,
                "diff_lines": self.performance.diff_lines,
                "planning_time": self.performance.planning_time,
                "validation_time": self.performance.validation_time,
                "tool_calls": self.performance.tool_calls,
                "commits_created": self.performance.commits_created,
            },
            "failure_modes": [fm.value for fm in self.failure_modes],
        }


@dataclass
class BenchmarkReport:
    """Complete benchmark evaluation report."""
    revision: str
    timestamp: str
    total_cases: int
    passed: int
    failed: int
    blocked: int
    overall_score: float
    results: List[EvaluationResult]
    failures_by_class: Dict[str, int]
    failures_by_mode: Dict[str, int]
    average_runtime: float
    average_retries: float
    average_files_changed: float
    
    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps({
            "revision": self.revision,
            "timestamp": self.timestamp,
            "summary": {
                "total_cases": self.total_cases,
                "passed": self.passed,
                "failed": self.failed,
                "blocked": self.blocked,
                "overall_score": self.overall_score,
                "average_runtime": self.average_runtime,
                "average_retries": self.average_retries,
                "average_files_changed": self.average_files_changed,
            },
            "failures_by_class": self.failures_by_class,
            "failures_by_mode": self.failures_by_mode,
            "cases": [r.to_dict() for r in self.results],
        }, indent=2)
