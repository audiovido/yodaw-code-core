"""
Elite Intelligence Benchmark Lab.

Hermetic, reproducible benchmarks for the intelligence layer itself:
classification accuracy, skill selection, repo profiling, framework
detection, review-loop defect catching, risk detection, context
selection, strategy selection, false-PASS prevention, and
multi-language handling.

No paid APIs, no network: every case is a pure function over
in-memory or temp-dir inputs.

The headline metric: CORRECT SOLUTION WITH ZERO FALSE PASS.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from app.intelligence.capability_registry import (
    CapabilityRegistry,
    DEFAULT_CAPABILITIES,
    capabilities_for_language,
    capabilities_for_repo,
)
from app.intelligence.context_builder import RepoContextBuilder
from app.intelligence.debug_engine import (
    DebugSession,
    apply_experiment_result,
    cheapest_discriminating,
    generate_hypotheses,
    rank_hypotheses,
)
from app.intelligence.language_adaptation import (
    adaptation_for_language,
    format_language_guidance,
)
from app.intelligence.quality_gate import assess_quality
from app.intelligence.repo_profiler import RepoProfiler
from app.intelligence.review_engine import verify_edit_content
from app.intelligence.task_strategy import (
    BUG_FIX,
    CONCURRENCY_BUG,
    DATABASE_TASK,
    UI_TASK,
    classify_task,
)


@dataclass
class EliteBenchCase:
    """One intelligence benchmark case."""

    id: str
    title: str
    fn: Callable[[], List[str]]      # returns [] on pass, defect strings otherwise
    family: str = "intelligence"
    tags: List[str] = field(default_factory=list)


@dataclass
class EliteBenchResult:
    case_id: str
    title: str
    passed: bool
    findings: List[str]
    elapsed_ms: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EliteBenchReport:
    total: int
    passed: int
    failed: int
    false_pass_rate: float          # failed-with-pass-style defects / total
    results: List[EliteBenchResult]

    def summary(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "zero_false_pass": self.false_pass_rate == 0.0,
            "difficulty": "hermetic-local",
        }


def _write_repo(files: Dict[str, str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="elite_bench_"))
    for rel, content in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def _case_classify_bug_fix() -> List[str]:
    sel = classify_task("Fix the checkout race condition in the payment service")
    if sel.task_type != CONCURRENCY_BUG:
        return [f"expected concurrency_bug, got {sel.task_type}"]
    return []


def _case_classify_security() -> List[str]:
    sel = classify_task("Close the SQL injection in the login endpoint")
    if sel.task_type != "security":
        return [f"expected security, got {sel.task_type}"]
    if "security" not in sel.required_capabilities:
        return ["security skill missing from required capabilities"]
    return []


def _case_classify_ui() -> List[str]:
    profile = {"primary_language": "typescript", "frameworks": ["react", "nextjs"]}
    sel = classify_task("Make the checkout button open the modal on click", profile)
    if sel.task_type != UI_TASK:
        return [f"expected ui_task, got {sel.task_type}"]
    if "react" not in sel.required_capabilities:
        return ["react not required for UI task in react repo"]
    return []


def _case_classify_db() -> List[str]:
    profile = {"primary_language": "python", "frameworks": ["fastapi"]}
    sel = classify_task("Add a unique constraint migration to the users table", profile)
    if sel.task_type != DATABASE_TASK:
        return [f"expected database_task, got {sel.task_type}"]
    return []


def _case_capability_taxonomy() -> List[str]:
    reg = CapabilityRegistry()
    if reg.count() < 50:
        return [f"taxonomy too small: {reg.count()} capabilities"]
    families = reg.families()
    for required in ("frontend", "backend", "database", "mobile", "systems", "cloud", "engineering"):
        if required not in families:
            return [f"missing family {required}"]
    # Extensibility: unknown framework key must be addable.
    from app.intelligence.capability_registry import Capability

    reg.register(
        Capability("my_framework", "backend", "My Framework", languages=("python",))
    )
    if reg.get("my_framework") is None:
        return ["registry extension failed"]
    return []


def _case_profile_python_repo() -> List[str]:
    repo = _write_repo(
        {
            "pyproject.toml": "[tool.poetry.dependencies]\npytest = \"*\"\n",
            "fastapi_app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "tests/test_x.py": "def test_x(): pass\n",
            "main.py": "import uvicorn\n",
            ".env": "DATABASE_URL=postgres://u:p@h/db\n",
        }
    )
    try:
        profiler = RepoProfiler()
        profile = profiler.to_dict(profiler.profile(repo))
        if profile["primary_language"] != "python":
            return [f"primary language mismatch: {profile['primary_language']}"]
        if "fastapi" not in profile["frameworks"]:
            return ["fastapi not detected"]
        if "pytest" not in profile["frameworks"]:
            return ["pytest not detected"]
        if not profile["test_directories"]:
            return ["tests dir not detected"]
        if ".env" not in profile["dangerous_files"]:
            return ["dangerous file .env not flagged"]
        return []
    finally:
        import shutil

        shutil.rmtree(repo.parent, ignore_errors=True)


def _case_profile_ts_repo() -> List[str]:
    repo = _write_repo(
        {
            "package.json": json.dumps(
                {
                    "dependencies": {
                        "react": "^18",
                        "next": "^14",
                        "typescript": "^5",
                    },
                    "devDependencies": {"jest": "^29"},
                }
            ),
            "tsconfig.json": "{}",
            ".eslintrc.json": "{}",
            "src/index.tsx": "export default function App() { return null; }",
        }
    )
    try:
        profiler = RepoProfiler()
        profile = profiler.to_dict(profiler.profile(repo))
        if profile["primary_language"] not in ("typescript", "javascript"):
            return [f"expected ts/js primary, got {profile['primary_language']}"]
        for fw in ("react", "nextjs"):
            if fw not in profile["frameworks"]:
                return [f"{fw} not detected in package.json"]
        if "eslint" not in profile["linters"]:
            return ["eslint not detected"]
        if "npm" not in profile["build_tools"]:
            return ["npm not detected"]
        return []
    finally:
        import shutil

        shutil.rmtree(repo.parent, ignore_errors=True)


def _case_multi_language_repo() -> List[str]:
    repo = _write_repo(
        {
            "pyproject.toml": "[project]\n",
            "backend.py": "def handler(): return 1\n",
            "package.json": "{}",
            "frontend.ts": "export const x = 1;\n",
            "go.mod": "module example.com/x\n\ngo 1.21\n",
            "main.go": "package main\n",
        }
    )
    try:
        profiler = RepoProfiler()
        profile = profiler.to_dict(profiler.profile(repo))
        langs = set(profile["language_breakdown"].keys())
        for lang in ("python", "typescript", "go"):
            if lang not in langs:
                return [f"{lang} missing from language breakdown: {langs}"]
        return []
    finally:
        import shutil

        shutil.rmtree(repo.parent, ignore_errors=True)


def _case_review_catches_placeholder() -> List[str]:
    verdict = verify_edit_content(
        "implement feature",
        [{"target_file": "app.py", "find": "x", "replace": "raise NotImplementedError"}],
        test_success=True,
    )
    if verdict.passed:
        return ["placeholder implementation passed review"]
    return []


def _case_review_catches_test_weakening() -> List[str]:
    findings = verify_edit_content(
        "fix failing test",
        [{"target_file": "test_x.py", "find": "a", "replace": "pass  # skipped"}],
        test_success=True,
        diff_text="+    @pytest.mark.skip(reason='flaky')\n+    def test_x():\n+    assert 1 == 1\n",
    )
    if findings.passed:
        return ["test weakening passed review"]
    return []


def _case_review_catches_scope_creep() -> List[str]:
    edits = [
        {"target_file": f"file_{i}.py", "find": f"v{i}", "replace": "z"}
        for i in range(25)
    ]
    verdict = verify_edit_content("small refactor", edits, test_success=True)
    if verdict.passed:
        return ["25-file scope creep passed review"]
    return []


def _case_review_passes_clean() -> List[str]:
    verdict = verify_edit_content(
        "fix off-by-one",
        [{"target_file": "calc.py", "find": "range(n)", "replace": "range(n+1)"}],
        test_success=True,
    )
    if not verdict.passed:
        return [f"clean minimal change rejected: {verdict.summary}"]
    if not verdict.warnings():
        pass  # clean is clean
    return []


def _case_debug_hypotheses() -> List[str]:
    session = DebugSession(
        goal="fix timeout",
        failure_snippet="test timed out after 30s: lock contention in worker pool",
    )
    hyps = generate_hypotheses(session.failure_snippet, session.goal)
    session.hypotheses = hyps
    if len(hyps) < 2:
        return ["not enough hypotheses generated"]
    ranked = rank_hypotheses(hyps)
    if ranked[0].score() < ranked[-1].score():
        return ["rank order wrong"]
    target = cheapest_discriminating(hyps)
    if target is None:
        return ["no discriminating hypothesis available"]
    apply_experiment_result(session, target.id, False, "ran repro with lock removed")
    if target.status != "eliminated":
        return ["eliminated hypothesis not marked"]
    # Hypothesis loop keeps going to the next candidate after elimination.
    next_hyp = cheapest_discriminating(hyps)
    if next_hyp is None:
        return ["no hypothesis left after elimination despite more pending"]
    apply_experiment_result(session, next_hyp.id, True, "instrumented wait loop")
    if not session.concluded:
        return ["session did not conclude after confirmed hypothesis"]
    return []


def _case_quality_gate_rewrite() -> List[str]:
    diff = "".join(f"+ line {i} changed\n- old {i}\n" for i in range(300))
    report = assess_quality(diff)
    if report.score > 0.55:
        return [f"large rewrite not penalized: score={report.score}"]
    return []


def _case_quality_gate_clean() -> List[str]:
    diff = "+    return a + b\n-    return a - b\n"
    report = assess_quality(diff)
    if report.verdict == "reject":
        return ["clean diff rejected"]
    return []


def _case_context_budget() -> List[str]:
    repo = _write_repo(
        {
            "main.py": "def main():\n    return checkout(1)\n",
            "checkout.py": "def checkout(user_id):\n    return {'ok': True}\n",
            "unrelated/" + "x" * 50 + ".py": "def noise(): return 0\n",
            "big_blob.py": "y = 1\n" * 20_000,
        }
    )
    try:
        builder = RepoContextBuilder(max_chars=8000)
        build = builder.build("fix checkout behavior", repo)
        if not build.included_files:
            return ["context builder included nothing"]
        if "checkout.py" not in build.included_files:
            return ["highly relevant file omitted from context"]
        sect_count = sum(len(v) for v in build.sections.values())
        if sect_count > 24000:
            return [f"context exceeds budget: {sect_count}"]
        if not build.ranked_files:
            return ["no ranked files"]
        if build.strategy is None:
            return ["strategy missing from context"]
        return []
    finally:
        import shutil

        shutil.rmtree(repo.parent, ignore_errors=True)


def _case_zero_false_pass() -> List[str]:
    """The headline metric: a reviewer must never pass a change that
    contains a blocking defect."""
    plant = [
        {"target_file": "a.py", "find": "x", "replace": "raise NotImplementedError"},
        {"target_file": "b.py", "find": "y", "replace": "SECRET='sk-live-abcdefghij123456'"},
    ]
    verdict = verify_edit_content("task", plant, test_success=True)
    if verdict.passed:
        return ["FALSE PASS: blocker defect passed review"]
    return []


def _case_language_gotchas() -> List[str]:
    text = format_language_guidance(
        {"primary_language": "typescript", "frameworks": ["react", "nextjs"]}
    )
    if "tsconfig" not in text:
        return ["typescript guidance missing"]
    if "use client" not in text:
        return ["nextjs gotcha missing"]
    adapt = adaptation_for_language("go")
    if not adapt or not any("go test" in t for t in adapt["toolchain"]["test"]):
        return ["go adaptation missing"]
    return []


def _case_capability_selection() -> List[str]:
    profile = {
        "primary_language": "python",
        "secondary_languages": ["javascript", "typescript"],
        "frameworks": ["fastapi", "react"],
    }
    caps = capabilities_for_repo(profile)
    keys = {c.key for c in caps}
    for required in ("python", "fastapi", "react", "typescript", "javascript"):
        if required not in keys:
            return [f"capability {required} not selected"]
    langs = capabilities_for_language("rust")
    if not langs:
        return ["rust has no capabilities"]
    return []


# ---------------------------------------------------------------------------
# Suite
# ---------------------------------------------------------------------------

ELITE_CASES: List[EliteBenchCase] = [
    EliteBenchCase("classify_race", "race condition classification", _case_classify_bug_fix, tags=["classification"]),
    EliteBenchCase("classify_security", "security task classification", _case_classify_security, tags=["classification"]),
    EliteBenchCase("classify_ui", "UI task classification with repo evidence", _case_classify_ui, tags=["classification"]),
    EliteBenchCase("classify_db", "database task classification", _case_classify_db, tags=["classification"]),
    EliteBenchCase("taxonomy", "universal capability taxonomy", _case_capability_taxonomy, tags=["taxonomy"]),
    EliteBenchCase("profile_python", "python repo profiling", _case_profile_python_repo, tags=["profiling"]),
    EliteBenchCase("profile_ts", "typescript repo profiling", _case_profile_ts_repo, tags=["profiling"]),
    EliteBenchCase("profile_multi", "multi-language repo profiling", _case_multi_language_repo, tags=["profiling"]),
    EliteBenchCase("review_placeholder", "review catches placeholder", _case_review_catches_placeholder, tags=["review"]),
    EliteBenchCase("review_test_weakening", "review catches test weakening", _case_review_catches_test_weakening, tags=["review"]),
    EliteBenchCase("review_scope", "review catches scope creep", _case_review_catches_scope_creep, tags=["review"]),
    EliteBenchCase("review_clean", "review passes clean change", _case_review_passes_clean, tags=["review"]),
    EliteBenchCase("debug_loop", "hypothesis-driven debug loop", _case_debug_hypotheses, tags=["debugging"]),
    EliteBenchCase("quality_rewrite", "quality gate penalizes rewrite", _case_quality_gate_rewrite, tags=["quality"]),
    EliteBenchCase("quality_clean", "quality gate accepts clean diff", _case_quality_gate_clean, tags=["quality"]),
    EliteBenchCase("context_budget", "progressive context selects relevant files", _case_context_budget, tags=["context"]),
    EliteBenchCase("zero_false_pass", "ZERO FALSE PASS guard", _case_zero_false_pass, tags=["false-pass"]),
    EliteBenchCase("language_gotchas", "language/framework adaptation", _case_language_gotchas, tags=["adaptation"]),
    EliteBenchCase("capability_selection", "repo-driven capability selection", _case_capability_selection, tags=["selection"]),
]


def run_elite_benchmarks(case_filter: Optional[str] = None) -> EliteBenchReport:
    results: List[EliteBenchResult] = []
    for case in ELITE_CASES:
        if case_filter and case_filter not in case.id:
            continue
        start = time.monotonic()
        try:
            findings = case.fn()
            passed = not findings
        except Exception as exc:  # benchmark infra never crashes the suite
            findings = [f"exception: {type(exc).__name__}: {exc}"]
            passed = False
        elapsed = (time.monotonic() - start) * 1000
        results.append(
            EliteBenchResult(
                case_id=case.id,
                title=case.title,
                passed=passed,
                findings=findings,
                elapsed_ms=round(elapsed, 1),
            )
        )
    passed_count = sum(1 for r in results if r.passed)
    # Zero false pass: for cases whose job is to block a planted defect,
    # a "false pass" is the gate failing to block (case reports findings,
    # i.e. passed=False). The headline metric is that no planted defect
    # ever gets through the gate.
    defect_cases = {
        "review_placeholder",
        "review_test_weakening",
        "review_scope",
        "quality_rewrite",
        "zero_false_pass",
    }
    false_passes = sum(
        1 for r in results if r.case_id in defect_cases and not r.passed
    )
    return EliteBenchReport(
        total=len(results),
        passed=passed_count,
        failed=len(results) - passed_count,
        false_pass_rate=false_passes / max(len(results), 1),
        results=results,
    )


def print_elite_report(report: EliteBenchReport) -> None:
    print("\n" + "=" * 64)
    print("YODAW ELITE INTELLIGENCE BENCHMARK")
    print("=" * 64)
    print(f"Total cases: {report.total}")
    print(f"Passed:      {report.passed}")
    print(f"Failed:      {report.failed}")
    print(f"Zero false pass: {'YES' if report.false_pass_rate == 0 else 'NO'}")
    print("-" * 64)
    for r in report.results:
        marker = "PASS" if r.passed else "FAIL"
        print(f"  [{marker}] {r.case_id:24s} {r.title} ({r.elapsed_ms:.0f}ms)")
        for f in r.findings[:3]:
            print(f"        - {f}")
    print("=" * 64)


if __name__ == "__main__":
    import sys

    filt = sys.argv[1] if len(sys.argv) > 1 else None
    rep = run_elite_benchmarks(filt)
    print_elite_report(rep)
    sys.exit(0 if rep.failed == 0 else 1)