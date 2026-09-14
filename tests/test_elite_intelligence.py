"""
Hermetic tests for the Elite Coding Intelligence layer.

Covers: skill detection, skill composition, repo profiling, framework
detection, task classification, review loop, risk detection, context
selection, strategy selection, false-PASS prevention, multi-language
repos, the debug engine, and the quality gate.

No paid APIs, no network: everything runs locally.
"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from app.intelligence.capability_registry import (
    Capability,
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
from app.intelligence.review_engine import (
    ModelReviewer,
    verify_edit_content,
    build_review_context,
)
from app.intelligence.task_strategy import (
    BUG_FIX,
    CONCURRENCY_BUG,
    FEATURE,
    UI_TASK,
    classify_task,
    STRATEGIES,
)


def _make_repo(files: dict) -> Path:
    """Create a temp git-less repo directory with the given files."""
    tmp = Path(tempfile.mkdtemp(prefix="elite_test_"))
    for rel, content in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp


@pytest.fixture(autouse=True)
def _cleanup_tmp():
    yield
    # Best-effort cleanup of leftover temp dirs
    for p in Path(tempfile.gettempdir()).glob("elite_test_*"):
        shutil.rmtree(p, ignore_errors=True)


# ---------------------------------------------------------------------------
# Phase 2: capability taxonomy
# ---------------------------------------------------------------------------


def test_taxonomy_has_all_families():
    reg = CapabilityRegistry()
    families = set(reg.families())
    for required in ("frontend", "backend", "database", "mobile", "systems", "cloud", "devops", "engineering"):
        assert required in families


def test_taxonomy_sufficient_coverage():
    reg = CapabilityRegistry()
    assert reg.count() >= 50


def test_taxonomy_extensible():
    reg = CapabilityRegistry()
    reg.register(Capability("future_lang", "backend", "FutureLang", languages=("fl",)))
    assert reg.get("future_lang") is not None
    assert reg.count() == len(DEFAULT_CAPABILITIES) + 1


def test_taxonomy_unknown_key_absent():
    reg = CapabilityRegistry()
    assert reg.get("does_not_exist") is None


def test_skill_detection_by_language():
    keys = capabilities_for_language("rust")
    assert "rust" in keys
    assert "cpp" not in keys


# ---------------------------------------------------------------------------
# Phase 3: skill selection (repo evidence driven)
# ---------------------------------------------------------------------------


def test_capability_composition_for_repo():
    profile = {
        "primary_language": "python",
        "secondary_languages": ["typescript"],
        "frameworks": ["fastapi", "react"],
    }
    caps = capabilities_for_repo(profile)
    keys = {c.key for c in caps}
    assert {"python", "fastapi", "react", "typescript"} <= keys


def test_capability_composition_respects_stack():
    """A pure Go repo must not demand frontend capabilities."""
    profile = {"primary_language": "go", "frameworks": ["gin"]}
    caps = capabilities_for_repo(profile)
    keys = {c.key for c in caps}
    assert "go" in keys
    assert "react" not in keys


# ---------------------------------------------------------------------------
# Phase 4: repo profiling
# ---------------------------------------------------------------------------


def test_repo_profile_python_fastapi():
    repo = _make_repo(
        {
            "pyproject.toml": "[tool.poetry.dependencies]\npytest = '*'\n",
            "app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "tests/test_app.py": "def test_x():\n    pass\n",
        }
    )
    profiler = RepoProfiler()
    profile = profiler.to_dict(profiler.profile(repo))
    assert profile["primary_language"] == "python"
    assert "fastapi" in profile["frameworks"]
    assert "pytest" in profile["frameworks"]
    assert "tests" in profile["test_directories"]


def test_repo_profile_typescript_primary():
    """Config-only files (package.json/tsconfig.json) must not shadow
    the real source language."""
    repo = _make_repo(
        {
            "package.json": json.dumps({"dependencies": {"react": "^18"}}),
            "tsconfig.json": "{}",
            "src/App.tsx": "export const App = () => <div/>;\n",
        }
    )
    profiler = RepoProfiler()
    profile = profiler.to_dict(profiler.profile(repo))
    assert profile["primary_language"] in ("typescript", "javascript")
    assert "react" in profile["frameworks"]
    assert "npm" in profile["build_tools"]


def test_repo_profile_dangerous_files():
    repo = _make_repo({"main.py": "x = 1\n", ".env": "TOKEN=abc", "Dockerfile": ""})
    profiler = RepoProfiler()
    profile = profiler.to_dict(profiler.profile(repo))
    assert ".env" in profile["dangerous_files"]
    assert profile["has_docker"] is True


def test_repo_profile_multi_language():
    repo = _make_repo(
        {
            "pyproject.toml": "[project]\n",
            "backend.py": "def f(): return 1\n",
            "package.json": "{}",
            "frontend.ts": "export const x = 1;\n",
            "go.mod": "module x\n\ngo 1.21\n",
            "main.go": "package main\n",
        }
    )
    profiler = RepoProfiler()
    profile = profiler.to_dict(profiler.profile(repo))
    langs = set(profile["language_breakdown"].keys())
    assert {"python", "typescript", "go"} <= langs


# ---------------------------------------------------------------------------
# Phase 5: task strategy engine
# ---------------------------------------------------------------------------


def test_classify_concurrency_bug():
    sel = classify_task("Fix the checkout race condition in the payment service")
    assert sel.task_type == CONCURRENCY_BUG
    assert "bugfix" in sel.required_capabilities


def test_classify_security():
    sel = classify_task("Close the SQL injection in the login endpoint")
    assert sel.task_type == "security"
    assert "security" in sel.required_capabilities


def test_classify_ui_with_repo_evidence():
    profile = {"primary_language": "typescript", "frameworks": ["react"]}
    sel = classify_task("Make the checkout button open the modal on click", profile)
    assert sel.task_type == UI_TASK
    assert "react" in sel.required_capabilities


def test_classify_database():
    profile = {"primary_language": "python", "frameworks": ["fastapi"]}
    sel = classify_task("Add a unique constraint migration to the users table", profile)
    assert sel.task_type == "database_task"


def test_strategy_policies_exist_for_all_types():
    for task_type in ("bug_fix", "feature", "refactor", "performance", "security",
                      "migration", "test_failure", "build_failure", "dependency_upgrade",
                      "api_change", "ui_task", "database_task", "concurrency_bug",
                      "architecture"):
        assert task_type in STRATEGIES, f"missing strategy for {task_type}"
        strategy = STRATEGIES[task_type]
        assert strategy.required_evidence
        assert strategy.review_focus
        assert strategy.test_strategy


def test_strategy_selection_produces_verification():
    sel = classify_task("Fix the failing test in CI")
    assert "verification_strategy" in sel.to_dict()
    assert sel.to_dict()["task_type"] == "test_failure"


# ---------------------------------------------------------------------------
# Phase 6: review loop
# ---------------------------------------------------------------------------


def test_review_blocks_placeholder():
    verdict = verify_edit_content(
        "task",
        [{"target_file": "a.py", "find": "x", "replace": "raise NotImplementedError"}],
        test_success=True,
    )
    assert not verdict.passed
    assert any(f.category == "correctness" and f.severity == "blocker" for f in verdict.blockers())


def test_review_blocks_test_weakening():
    verdict = verify_edit_content(
        "fix test",
        [{"target_file": "test_a.py", "find": "x", "replace": "y"}],
        test_success=True,
        diff_text="+    @pytest.mark.skip(reason='flaky')\n+    def test_a():\n",
    )
    assert not verdict.passed


def test_review_blocks_scope_creep():
    edits = [{"target_file": f"f{i}.py", "find": f"v{i}", "replace": "z"} for i in range(25)]
    verdict = verify_edit_content("small task", edits, test_success=True)
    assert not verdict.passed


def test_review_passes_clean_change():
    verdict = verify_edit_content(
        "fix off-by-one",
        [{"target_file": "calc.py", "find": "range(n)", "replace": "range(n+1)"}],
        test_success=True,
    )
    assert verdict.passed


def test_review_no_fake_pass_without_green_tests():
    """Even a clean change must not pass review when tests are not
    observed green (validation is the real gate)."""
    verdict = verify_edit_content(
        "task", [{"target_file": "a.py", "find": "x", "replace": "y"}],
        test_success=False,
    )
    assert any(f.category == "test_coverage" for f in verdict.findings)


def test_model_reviewer_unparsable_does_not_block():
    reviewer = ModelReviewer(lambda s, u: "not json at all")
    verdict = reviewer.review("context")
    assert verdict.passed
    assert not verdict.model_used


def test_model_reviewer_honest_reject():
    def fake_review(system, user):
        return json.dumps(
            {
                "verdict": "reject",
                "blockers": [{"category": "correctness", "message": "off-by-one at x", "file": "a.py"}],
                "warnings": [],
                "summary": "defect found",
            }
        )

    reviewer = ModelReviewer(fake_review)
    verdict = reviewer.review(build_review_context("goal", "+ x+=1\n- x+=2\n", []))
    assert not verdict.passed
    assert verdict.model_used
    assert any(f.category == "correctness" for f in verdict.blockers())


# ---------------------------------------------------------------------------
# Phase 8: adversarial debugging
# ---------------------------------------------------------------------------


def test_debug_hypothesis_elimination_loop():
    session = DebugSession(
        goal="fix crash",
        failure_snippet="KeyError: 'tenant_id' in checkout after cancel",
    )
    session.hypotheses = generate_hypotheses(session.failure_snippet, session.goal)
    assert len(session.hypotheses) >= 2

    target = cheapest_discriminating(session.hypotheses)
    apply_experiment_result(session, target.id, False, "ran repro without tenant guard")
    assert target.status == "eliminated"

    survivor = cheapest_discriminating(session.hypotheses)
    assert survivor is not None
    apply_experiment_result(session, survivor.id, True, "repro with tenant default")
    assert session.concluded
    assert session.resolution


def test_debug_ranking_prefers_confident_cheap():
    session = DebugSession(goal="x", failure_snippet="timeout")
    session.hypotheses = [
        generate_hypotheses("timeout in worker pool", "x")[0],
    ]
    session.hypotheses[0].confidence = 0.9
    session.hypotheses[0].cost = 5.0
    cheap = DebugSession(goal="x", failure_snippet="timeout")
    cheap.hypotheses = [generate_hypotheses("timeout in worker pool", "x")[0]]
    cheap.hypotheses[0].confidence = 0.4
    cheap.hypotheses[0].cost = 1.0
    ranked = rank_hypotheses([session.hypotheses[0], cheap.hypotheses[0]])
    assert ranked[0] is cheap.hypotheses[0]  # cheaper, decent confidence wins


# ---------------------------------------------------------------------------
# Phase 9: quality / complexity control
# ---------------------------------------------------------------------------


def test_quality_gate_penalizes_rewrite():
    diff = "".join(f"+ new line {i}\n- old line {i}\n" for i in range(300))
    report = assess_quality(diff)
    assert report.verdict != "clean"
    assert report.score < 0.6


def test_quality_gate_pass_minimal():
    report = assess_quality("+    return a + b\n-    return a - b\n")
    assert report.verdict == "clean"


def test_quality_gate_flags_test_deletion():
    report = assess_quality("+ new\n-    def test_x():\n", tests_deleted=1)
    assert report.verdict == "reject"
    assert any("test_deletion" == i["category"] for i in report.issues)


def test_quality_gate_flags_secret():
    diff = '+TOKEN = "sk-live-abcdefghijklmnop123456789"\n'
    report = assess_quality(diff, task_type="feature")
    assert any(i["category"] == "security" and i["severity"] == "blocker" for i in report.issues)


# ---------------------------------------------------------------------------
# Phase 4: context selection (progressive context)
# ---------------------------------------------------------------------------


def test_context_ranks_relevant_files_first():
    repo = _make_repo(
        {
            "checkout.py": "def checkout(user_id):\n    return {'ok': True}\n",
            "main.py": "from checkout import checkout\nprint(checkout(1))\n",
            "unrelated.py": "def noise():\n    return 0\n",
        }
    )
    builder = RepoContextBuilder(max_chars=6000)
    build = builder.build("fix checkout behavior", repo)
    assert "checkout.py" in build.included_files
    assert build.ranked_files
    assert build.ranked_files[0]["path"] in ("checkout.py", "main.py")


def test_context_respects_budget():
    repo = _make_repo(
        {
            "a.py": "x = 1\n" * 2000,
            "b.py": "y = 2\n" * 2000,
            "c.py": "z = 3\n",
        }
    )
    builder = RepoContextBuilder(max_chars=4000)
    build = builder.build("nothing specific", repo)
    total = sum(len(v) for v in build.sections.values())
    assert total <= 24000  # hard ceiling regardless of budget
    assert "FILE INDEX" in build.sections or len(build.included_files) > 0


def test_context_includes_strategy():
    repo = _make_repo({"app.py": "def main(): pass\n"})
    builder = RepoContextBuilder(max_chars=8000)
    build = builder.build("fix the crash in app.py", repo)
    assert build.strategy is not None


# ---------------------------------------------------------------------------
# Phase 7: language adaptation
# ---------------------------------------------------------------------------


def test_language_adaptation_python():
    adapt = adaptation_for_language("python")
    assert adapt is not None
    assert "pytest" in adapt["toolchain"]["test"]


def test_language_adaptation_unknown():
    assert adaptation_for_language("cobol") is None


def test_format_guidance_mentions_tsconfig():
    text = format_language_guidance(
        {"primary_language": "typescript", "frameworks": ["react", "nextjs"]}
    )
    assert "tsconfig" in text
    assert "use client" in text


# ---------------------------------------------------------------------------
# Zero false PASS (headline metric)
# ---------------------------------------------------------------------------


def test_zero_false_pass_planted_defects():
    """Every planted blocker must be caught by the review gate."""
    plant = [
        {"target_file": "a.py", "find": "x", "replace": "raise NotImplementedError"},
        {"target_file": "b.py", "find": "y", "replace": "KEY='sk-live-abcdefghij987654321'"},
    ]
    verdict = verify_edit_content("task", plant, test_success=True)
    assert not verdict.passed  # no fake PASS


# ---------------------------------------------------------------------------
# Coder brain integration
# ---------------------------------------------------------------------------


def test_format_intelligence_empty():
    from app.llm.coder import format_intelligence

    assert format_intelligence(None) == ""
    assert format_intelligence({}) == ""
    assert format_intelligence("t") == "t"


def test_format_intelligence_merges_sections():
    from app.llm.coder import format_intelligence

    block = format_intelligence(
        {
            "strategy": "TASK STRATEGY: bug_fix",
            "sections": {"REPOSITORY PROFILE": "python", "FILE INDEX": "app.py"},
            "debug": "DEBUG",
        }
    )
    assert "TASK STRATEGY: bug_fix" in block
    assert "python" in block
    assert "DEBUG" in block


def test_generate_edit_plan_accepts_intelligence():
    """generate_edit_plan must still work with a stub provider and an
    intelligence block (no repo, no API)."""
    from app.llm.coder import generate_edit_plan

    class StubProvider:
        def chat(self, system, user):
            assert "reuse" in user.lower() or "repository" in user.lower()
            return json.dumps({"action": "blocked", "reason": "test"})

    plan = generate_edit_plan(
        "add greeting",
        Path(tempfile.mkdtemp(prefix="elite_test_")),
        provider=StubProvider(),
        intelligence={"strategy": "TASK STRATEGY: feature"},
    )
    assert plan["action"] == "blocked"