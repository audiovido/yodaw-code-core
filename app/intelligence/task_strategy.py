"""
Task Strategy Engine.

Recognizes task categories (bug fix, feature, refactor, performance,
security, migration, ...) and assigns each category an explicit
execution strategy: analysis depth, required evidence, test strategy,
risk checks, and review focus.

Combines:
- goal/description analysis (intent patterns, error logs, changed files)
- repository evidence (languages, frameworks, tests)

Phase 5 of the Elite Coding Intelligence layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.skills.models import Intent

# Canonical task categories.
BUG_FIX = "bug_fix"
FEATURE = "feature"
REFACTOR = "refactor"
PERFORMANCE = "performance"
SECURITY = "security"
MIGRATION = "migration"
TEST_FAILURE = "test_failure"
BUILD_FAILURE = "build_failure"
DEPENDENCY_UPGRADE = "dependency_upgrade"
API_CHANGE = "api_change"
UI_TASK = "ui_task"
DATABASE_TASK = "database_task"
CONCURRENCY_BUG = "concurrency_bug"
ARCHITECTURE = "architecture"
UNKNOWN = "unknown"

TASK_TYPES = (
    BUG_FIX,
    FEATURE,
    REFACTOR,
    PERFORMANCE,
    SECURITY,
    MIGRATION,
    TEST_FAILURE,
    BUILD_FAILURE,
    DEPENDENCY_UPGRADE,
    API_CHANGE,
    UI_TASK,
    DATABASE_TASK,
    CONCURRENCY_BUG,
    ARCHITECTURE,
    UNKNOWN,
)


@dataclass(frozen=True)
class TaskStrategy:
    """Execution policy for one task category."""

    task_type: str
    analysis_depth: str                 # "shallow" | "moderate" | "deep"
    required_evidence: tuple            # evidence kinds required before editing
    test_strategy: str
    risk_checks: tuple
    review_focus: tuple
    default_skills: tuple
    min_repair_budget: int = 1          # corrective attempts
    description: str = ""


STRATEGIES: Dict[str, TaskStrategy] = {
    BUG_FIX: TaskStrategy(
        task_type=BUG_FIX,
        analysis_depth="deep",
        required_evidence=("repro", "root_cause", "affected_files"),
        test_strategy="repro first (fails before, passes after) + full suite",
        risk_checks=("regressions", "edge_cases", "api_compat"),
        review_focus=("root_cause_match", "regression_free", "minimal_patch"),
        default_skills=("bugfix", "test", "review"),
        min_repair_budget=2,
        description="Locate root cause, minimal targeted fix, regression test.",
    ),
    CONCURRENCY_BUG: TaskStrategy(
        task_type=CONCURRENCY_BUG,
        analysis_depth="deep",
        required_evidence=("race_repro", "locks_audit", "state_mutation_points"),
        test_strategy="targeted stress/race repro + full suite",
        risk_checks=("deadlock", "ordering_changes", "atomicity"),
        review_focus=("atomicity", "lock_ordering", "no_sleep_fixes"),
        default_skills=("bugfix", "performance", "review"),
        min_repair_budget=2,
        description="Concurrency defects: races, deadlocks, torn writes.",
    ),
    FEATURE: TaskStrategy(
        task_type=FEATURE,
        analysis_depth="moderate",
        required_evidence=("acceptance_criteria", "integration_points"),
        test_strategy="new tests for feature + full suite",
        risk_checks=("scope_creep", "api_compat", "regressions"),
        review_focus=("acceptance_met", "conventions", "no_scope_creep"),
        default_skills=("feature", "test", "documentation", "review"),
        min_repair_budget=1,
        description="Implement new capability against acceptance criteria.",
    ),
    REFACTOR: TaskStrategy(
        task_type=REFACTOR,
        analysis_depth="deep",
        required_evidence=("baseline_tests", "behavior_map"),
        test_strategy="tests before + after; verify identical behavior",
        risk_checks=("behavior_change", "api_compat", "test_deletion"),
        review_focus=("behavior_preserved", "simpler", "incremental"),
        default_skills=("refactor", "test", "review"),
        min_repair_budget=2,
        description="Behavior-preserving restructuring under test.",
    ),
    PERFORMANCE: TaskStrategy(
        task_type=PERFORMANCE,
        analysis_depth="deep",
        required_evidence=("baseline_metrics", "bottleneck_profile"),
        test_strategy="benchmark before/after + full suite",
        risk_checks=("regressions", "memory", "correctness"),
        review_focus=("measured_gain", "no_premature_opt", "maintainable"),
        default_skills=("performance", "review", "feature"),
        min_repair_budget=2,
        description="Measured optimization of a real bottleneck.",
    ),
    SECURITY: TaskStrategy(
        task_type=SECURITY,
        analysis_depth="deep",
        required_evidence=("vuln_confirmation", "exploit_path"),
        test_strategy="exploit repro (blocked after fix) + full suite",
        risk_checks=("introduced_vulns", "secret_leak", "auth_bypass"),
        review_focus=("real_vuln_closed", "no_false_positive_fix", "minimal"),
        default_skills=("security", "review", "test"),
        min_repair_budget=2,
        description="Close a verified vulnerability without breaking behavior.",
    ),
    MIGRATION: TaskStrategy(
        task_type=MIGRATION,
        analysis_depth="deep",
        required_evidence=("rollback_plan", "data_impact", "compat_matrix"),
        test_strategy="migration + post-migration assertions + full suite",
        risk_checks=("data_loss", "rollback", "compat"),
        review_focus=("rollback_ready", "data_integrity", "phased"),
        default_skills=("migration", "review", "test"),
        min_repair_budget=2,
        description="Safe migration of code, schema, or dependencies.",
    ),
    TEST_FAILURE: TaskStrategy(
        task_type=TEST_FAILURE,
        analysis_depth="moderate",
        required_evidence=("failing_test", "failure_reason"),
        test_strategy="failing test green + full suite; never weaken assertion",
        risk_checks=("test_weakening", "masked_bug", "skip_deletion"),
        review_focus=("real_fix_not_weakened", "no_skips", "no_deletion"),
        default_skills=("bugfix", "test", "review"),
        min_repair_budget=2,
        description="Fix the code so the failing test passes on its merits.",
    ),
    BUILD_FAILURE: TaskStrategy(
        task_type=BUILD_FAILURE,
        analysis_depth="shallow",
        required_evidence=("build_log", "changed_files"),
        test_strategy="build green + full suite",
        risk_checks=("regressions", "lockfile_churn"),
        review_focus=("real_cause", "no_dependency_pin_jump"),
        default_skills=("bugfix", "dependency", "review"),
        min_repair_budget=2,
        description="Repair compile/build/CI breakage at its root cause.",
    ),
    DEPENDENCY_UPGRADE: TaskStrategy(
        task_type=DEPENDENCY_UPGRADE,
        analysis_depth="moderate",
        required_evidence=("version_matrix", "breaking_changes"),
        test_strategy="full suite + targeted integration checks",
        risk_checks=("breaking_change", "transitive", "lockfile"),
        review_focus=("compat_verified", "no_unrelated_upgrades"),
        default_skills=("dependency", "test", "review"),
        min_repair_budget=2,
        description="Upgrade dependencies with breaking-change analysis.",
    ),
    API_CHANGE: TaskStrategy(
        task_type=API_CHANGE,
        analysis_depth="deep",
        required_evidence=("contract_change", "callers_audit", "versioning_policy"),
        test_strategy="contract tests + caller regression + full suite",
        risk_checks=("callers_broken", "versioning", "backcompat"),
        review_focus=("callers_handled", "versioned", "documented"),
        default_skills=("feature", "refactor", "test", "review"),
        min_repair_budget=2,
        description="API contract change with caller compatibility handled.",
    ),
    UI_TASK: TaskStrategy(
        task_type=UI_TASK,
        analysis_depth="moderate",
        required_evidence=("component_map", "state_flow"),
        test_strategy="component/E2E checks + full suite",
        risk_checks=("regressions", "a11y", "responsive"),
        review_focus=("behavior_verified", "a11y", "no_inline_hacks"),
        default_skills=("feature", "refactor", "test", "review"),
        min_repair_budget=1,
        description="Frontend behavior change: state, layout, interaction.",
    ),
    DATABASE_TASK: TaskStrategy(
        task_type=DATABASE_TASK,
        analysis_depth="deep",
        required_evidence=("schema_state", "migration_path", "data_volume"),
        test_strategy="migration on empty + seeded data + full suite",
        risk_checks=("data_loss", "locking", "indexing"),
        review_focus=("idempotent_migration", "no_data_loss", "indexed"),
        default_skills=("migration", "bugfix", "test", "review"),
        min_repair_budget=2,
        description="Schema/data work: migrations, queries, indexing.",
    ),
    ARCHITECTURE: TaskStrategy(
        task_type=ARCHITECTURE,
        analysis_depth="deep",
        required_evidence=("boundary_map", "dependency_rules"),
        test_strategy="incremental + architectural tests + full suite",
        risk_checks=("coupling", "layering", "dead_code"),
        review_focus=("boundaries_respected", "no_duplication", "incremental"),
        default_skills=("refactor", "review", "test"),
        min_repair_budget=2,
        description="Structural change: layering, boundaries, patterns.",
    ),
    UNKNOWN: TaskStrategy(
        task_type=UNKNOWN,
        analysis_depth="moderate",
        required_evidence=("repo_scan", "affected_files"),
        test_strategy="full suite",
        risk_checks=("regressions",),
        review_focus=("correctness", "conventions"),
        default_skills=("feature", "review"),
        min_repair_budget=1,
        description="Unclassified task; default strategy with evidence.",
    ),
}


# Keyword signals per category, scored for classification.
TYPE_SIGNALS: Dict[str, tuple] = {
    BUG_FIX: (
        "race condition", "crash", "exception", "traceback", "not working",
        "wrong result", "incorrect", "broken", "fails", "error", "bug",
        "regression", "misbehav", "infinite loop", "null pointer",
        "keyerror", "typeerror", "attributeerror", "segfault", "panic",
    ),
    CONCURRENCY_BUG: (
        "race", "deadlock", "dead lock", "data race", "thread-safe",
        "thread safety", "concurr", "atomic", "torn", "starvation",
        "lock contention", "futex", "mutex",
    ),
    FEATURE: (
        "add ", "implement", "new feature", "support ", "introduce",
        "create", "build ", "develop", "enhance", "capability",
        "endpoint for", "command for",
    ),
    REFACTOR: (
        "refactor", "clean up", "cleanup", "restructure", "simplify",
        "extract", "deduplicate", "dedupe", "reduce complexity",
        "improve readability", "rename", "split module", "modularize",
    ),
    PERFORMANCE: (
        "slow", "performance", "latency", "throughput", "bottleneck",
        "optimize", "optimise", "profiling", "profile ", "timeout",
        "time out", "memory leak", "memory usage", "cpu usage", "n+1",
        "benchmark",
    ),
    SECURITY: (
        "security", "vulnerab", "exploit", "xss", "csrf", "injection",
        "sqli", "secret", "credential", "auth bypass", "privilege",
        "cwe-", "owasp",
    ),
    MIGRATION: (
        "migrat", "upgrade from", "port to", "move to ", "convert",
        "transpile", "legacy", "deprecat", "sunset", "strangler",
    ),
    TEST_FAILURE: (
        "failing test", "test failing", "test failure", "ci failing",
        "red test", "test broke", "test suite fails",
    ),
    BUILD_FAILURE: (
        "build fail", "compile error", "does not compile", "build error",
        "ci build", "syntax error", "import error", "module not found",
        "cannot find", "undefined reference", "linking error",
    ),
    DEPENDENCY_UPGRADE: (
        "upgrade dependenc", "bump ", "update dependenc", "update package",
        "upgrade to version", "new version of", "transitive dep",
        "lockfile", "vulnerable dependenc",
    ),
    API_CHANGE: (
        "api change", "endpoint change", "contract change", "rename api",
        "break api", "backward compat", "deprecate endpoint", "schema change",
        "response shape", "request shape",
    ),
    UI_TASK: (
        "button", "modal", "dropdown", "form field", "ui", "frontend",
        "component renders", "dark mode", "styling", "layout", "responsive",
        "navbar", "tooltip", "accordion", "toggle",
    ),
    DATABASE_TASK: (
        "migration", "schema", "database", "table", "index", "query slow",
        "sql", "postgres", "mysql", "sqlite", "mongo", "transaction",
        "unique constraint", "foreign key",
    ),
    ARCHITECTURE: (
        "architecture", "layering", "modular", "service boundary",
        "dependency inversion", "design pattern", "package structure",
        "monolith", "microservice",
    ),
}


def _score_type(goal: str, signals: tuple) -> float:
    text = goal.lower()
    score = 0.0
    for sig in signals:
        if sig in text:
            score += 1.0
            if text.startswith(sig):
                score += 0.5
    return score


@dataclass
class StrategySelection:
    """Result of task strategy selection."""

    task_type: str
    strategy: TaskStrategy
    intent: Intent
    required_capabilities: List[str] = field(default_factory=list)
    optional_capabilities: List[str] = field(default_factory=list)
    risk_areas: List[str] = field(default_factory=list)
    verification_strategy: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_type": self.task_type,
            "analysis_depth": self.strategy.analysis_depth,
            "required_capabilities": self.required_capabilities,
            "optional_capabilities": self.optional_capabilities,
            "risk_areas": self.risk_areas,
            "verification_strategy": self.verification_strategy,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "min_repair_budget": self.strategy.min_repair_budget,
        }


INTENT_BY_TYPE: Dict[str, Intent] = {
    BUG_FIX: Intent.BUGFIX,
    CONCURRENCY_BUG: Intent.BUGFIX,
    FEATURE: Intent.FEATURE,
    REFACTOR: Intent.REFACTOR,
    PERFORMANCE: Intent.PERFORMANCE,
    SECURITY: Intent.SECURITY,
    MIGRATION: Intent.MIGRATION,
    TEST_FAILURE: Intent.BUGFIX,
    BUILD_FAILURE: Intent.BUGFIX,
    DEPENDENCY_UPGRADE: Intent.DEPENDENCY,
    API_CHANGE: Intent.FEATURE,
    UI_TASK: Intent.FEATURE,
    DATABASE_TASK: Intent.MIGRATION,
    ARCHITECTURE: Intent.REFACTOR,
    UNKNOWN: Intent.FEATURE,
}


def classify_task(goal: str, profile: Optional[dict] = None) -> StrategySelection:
    """Classify a task into a strategy using goal text + repo evidence."""
    scores = {t: _score_type(goal, TYPE_SIGNALS[t]) for t in TYPE_SIGNALS}
    best = max(scores, key=scores.get)
    best_score = scores[best]

    if best_score == 0:
        best = UNKNOWN

    # Evidence from repo profile (frameworks / languages / test layout)
    profile = profile or {}
    evidence: Dict[str, Any] = {"signals": {}}
    confidence = min(0.4 + 0.15 * best_score, 0.97)

    # Domain override: database keywords push toward DATABASE_TASK.
    if best in (UNKNOWN, BUG_FIX, FEATURE, MIGRATION, REFACTOR):
        db_score = _score_type(goal, TYPE_SIGNALS[DATABASE_TASK])
        if db_score > 0 and db_score >= scores[best]:
            best = DATABASE_TASK
            confidence = min(0.4 + 0.15 * db_score, 0.97)

    # Concurrency override: any race/deadlock signal wins over generic bug.
    if best == BUG_FIX and _score_type(goal, TYPE_SIGNALS[CONCURRENCY_BUG]) > 0:
        best = CONCURRENCY_BUG
        confidence = min(0.4 + 0.15 * _score_type(goal, TYPE_SIGNALS[CONCURRENCY_BUG]), 0.97)

    for t, s in scores.items():
        if s > 0:
            evidence["signals"][t] = round(s, 2)

    strategy = STRATEGIES[best]
    lang = (profile.get("primary_language") or "").lower()

    required = list(strategy.default_skills)
    optional: List[str] = []
    risk_areas = list(strategy.risk_checks)

    # Framework/language-aware capability inference.
    if lang == "python":
        required.append("python")
    elif lang in ("typescript", "javascript", "node"):
        required.append("typescript" if lang == "typescript" else "javascript")
    for fw in profile.get("frameworks") or []:
        fw_lower = fw.lower()
        if fw_lower in ("react", "nextjs", "vue", "svelte", "angular"):
            optional.append(fw_lower)
            if best == UI_TASK:
                required.append(fw_lower)
        if fw_lower in ("fastapi", "django", "flask", "express", "nestjs"):
            if best in (BUG_FIX, FEATURE, API_CHANGE, TEST_FAILURE, DATABASE_TASK):
                required.append(fw_lower)
        if fw_lower in ("pytest", "jest", "vitest", "playwright"):
            optional.append("testing")

    # Repository risk areas.
    if profile.get("dangerous_files"):
        risk_areas.append("secrets_or_env_files_touched")
    if profile.get("generated_code"):
        risk_areas.append("generated_code_edit")

    # Verification strategy by type.
    verification = strategy.test_strategy
    if best == TEST_FAILURE:
        verification = "run failing test first; fix code; re-run; full suite green; no assertion weakening"
    elif best == BUILD_FAILURE:
        verification = "rebuild until green; then full test suite"
    elif best == UI_TASK:
        verification = "behavior exercised via tests; state transitions verified; full suite green"
    elif best == DATABASE_TASK:
        verification = "migration applied to empty + seeded DB; data assertions; full suite green"

    selection = StrategySelection(
        task_type=best,
        strategy=strategy,
        intent=INTENT_BY_TYPE[best],
        required_capabilities=list(dict.fromkeys(required)),
        optional_capabilities=list(dict.fromkeys(optional)),
        risk_areas=list(dict.fromkeys(risk_areas)),
        verification_strategy=verification,
        evidence=evidence,
        confidence=confidence,
    )
    return selection