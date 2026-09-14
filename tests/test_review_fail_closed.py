"""
P0 red-team regression: adversarial self-review must fail closed.

Covers the audited false-PASS chain where reviewer findings arrive as
typed ReviewFinding dataclasses but the blocker filter only accepted
plain dicts, allowing a mission to PASS and commit while
self_review.passed == False.

Cases:
- typed ReviewFinding blocker blocks PASS + commit
- non-blocker (warning) findings still allow PASS
- mixed warning+blocker findings block PASS
- reviewer exception fails closed (no PASS, no commit)
- verdict passed=False with empty findings still fails closed
- assert-weakening diff (assert expr -> assert True) blocks commit
- evidence agrees with mission state (no stale PASS evidence)
- plan-only / no-edit path cannot PASS
"""

import subprocess
from pathlib import Path

import pytest

import app.workers.repo_code_worker as worker_module
from app.intelligence.review_engine import ReviewFinding, ReviewVerdict
from app.workers.repo_code_worker import RepoCodeWorker


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repo(repo: Path, files: dict):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")
    for name, content in files.items():
        (repo / name).write_text(content)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return git(repo, "rev-parse", "HEAD")


def green_repo(tmp_path, name):
    repo = tmp_path / name
    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 1\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )
    return repo


def run_mission(repo, edits):
    return RepoCodeWorker().execute(
        "Green edit mission.",
        {
            "repo_path": str(repo),
            "edits": edits,
        },
    )


def result_error_types(result):
    return [
        item.get("type")
        for item in result["evidence"]
        if isinstance(item, dict)
    ]


def test_typed_review_blocker_dataclass_blocks_pass(tmp_path):
    repo = green_repo(tmp_path, "repo_blocker")
    baseline = git(repo, "rev-parse", "HEAD")

    def fake_review(goal, edits, strategy=None, diff_text="", test_success=False):
        return ReviewVerdict(
            passed=False,
            findings=[
                ReviewFinding(
                    "correctness",
                    "blocker",
                    "edit target outside plan scope",
                )
            ],
            summary="blocker: edit target outside plan scope",
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_module, "verify_edit_content", fake_review)
    try:
        result = run_mission(repo, [{"target_file": "calc.py", "find": "return 1", "replace": "return 1  # x"}])
    finally:
        monkeypatch.undo()

    assert result["success"] is False
    assert result["error"]["type"] == "ReviewBlocked"
    assert result["error"]["findings"][0]["severity"] == "blocker"
    assert result["error"]["findings"][0]["message"] == "edit target outside plan scope"
    assert git(repo, "rev-parse", "HEAD") == baseline
    self_reviews = [
        item for item in result["evidence"]
        if isinstance(item, dict) and item.get("type") == "self_review"
    ]
    assert self_reviews
    assert self_reviews[0]["passed"] is False


def test_review_non_blocker_warnings_still_allow_pass(tmp_path):
    repo = green_repo(tmp_path, "repo_warnings")

    def fake_review(goal, edits, strategy=None, diff_text="", test_success=False):
        return ReviewVerdict(
            passed=True,
            findings=[
                ReviewFinding("style", "warning", "debug output present"),
            ],
            summary="warning only",
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_module, "verify_edit_content", fake_review)
    try:
        result = run_mission(repo, [{"target_file": "calc.py", "find": "return 1", "replace": "return 1  # x"}])
    finally:
        monkeypatch.undo()

    assert result["success"] is True
    commit_sha = result["output"]["commit_sha"]
    assert commit_sha
    assert git(repo, "cat-file", "-e", commit_sha + "^{commit}") is not None
    assert result["output"]["branch"]


def test_mixed_warning_and_blocker_findings_block_pass(tmp_path):
    repo = green_repo(tmp_path, "repo_mixed")
    baseline = git(repo, "rev-parse", "HEAD")

    def fake_review(goal, edits, strategy=None, diff_text="", test_success=False):
        return ReviewVerdict(
            passed=False,
            findings=[
                ReviewFinding("style", "warning", "minor style nit"),
                ReviewFinding("api_compat", "blocker", "public signature changed"),
            ],
            summary="blocker: public signature changed",
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_module, "verify_edit_content", fake_review)
    try:
        result = run_mission(repo, [{"target_file": "calc.py", "find": "return 1", "replace": "return 1  # x"}])
    finally:
        monkeypatch.undo()

    assert result["success"] is False
    assert result["error"]["type"] == "ReviewBlocked"
    assert result["error"]["message"] == "public signature changed"
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_reviewer_exception_fails_closed(tmp_path):
    repo = green_repo(tmp_path, "repo_exc")
    baseline = git(repo, "rev-parse", "HEAD")

    def exploding_review(*args, **kwargs):
        raise RuntimeError("reviewer crashed")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_module, "verify_edit_content", exploding_review)
    try:
        result = run_mission(repo, [{"target_file": "calc.py", "find": "return 1", "replace": "return 1  # x"}])
    finally:
        monkeypatch.undo()

    assert result["success"] is False
    assert result["error"]["type"] == "ReviewBlocked"
    assert git(repo, "rev-parse", "HEAD") == baseline
    assert "self_review_error" in result_error_types(result)


def test_verdict_passed_false_with_no_findings_fails_closed(tmp_path):
    repo = green_repo(tmp_path, "repo_emptyverdict")
    baseline = git(repo, "rev-parse", "HEAD")

    def fake_review(goal, edits, strategy=None, diff_text="", test_success=False):
        return ReviewVerdict(
            passed=False,
            findings=[],
            summary="verdict failed but declared no findings",
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_module, "verify_edit_content", fake_review)
    try:
        result = run_mission(repo, [{"target_file": "calc.py", "find": "return 1", "replace": "return 1  # x"}])
    finally:
        monkeypatch.undo()

    assert result["success"] is False
    assert result["error"]["type"] == "ReviewBlocked"
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_assert_weakening_diff_blocks_commit(tmp_path):
    """Audit example: model rewrites `assert add(2,3)==5` into `assert True`.

    Real verify_edit_content (no seam) must flag the added `assert True`
    line as a test-weakening blocker and the mission must fail closed.
    """
    repo = tmp_path / "repo_weaken"
    init_repo(
        repo,
        {
            "calc.py": "def add(a, b):\n    return a + b\n",
            "test_calc.py": (
                "from calc import add\n\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )
    baseline = git(repo, "rev-parse", "HEAD")

    result = run_mission(
        repo,
        [{"target_file": "test_calc.py", "find": "assert add(2, 3) == 5", "replace": "assert True"}],
    )

    assert result["success"] is False
    assert result["error"]["type"] == "ReviewBlocked"
    assert "assertion" in result["error"]["message"].lower()
    assert git(repo, "rev-parse", "HEAD") == baseline
    self_reviews = [
        item for item in result["evidence"]
        if isinstance(item, dict) and item.get("type") == "self_review"
    ]
    assert self_reviews
    assert self_reviews[0]["passed"] is False


def test_evidence_agrees_with_mission_state_on_blocked_review(tmp_path):
    """No stale PASS evidence: blocked review => success False and no commit."""
    repo = green_repo(tmp_path, "repo_evidence")
    baseline = git(repo, "rev-parse", "HEAD")

    def fake_review(goal, edits, strategy=None, diff_text="", test_success=False):
        return ReviewVerdict(
            passed=False,
            findings=[ReviewFinding("no_change", "blocker", "edit is a no-op")],
            summary="blocker: edit is a no-op",
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_module, "verify_edit_content", fake_review)
    try:
        result = run_mission(repo, [{"target_file": "calc.py", "find": "return 1", "replace": "return 1  # x"}])
    finally:
        monkeypatch.undo()

    assert result["success"] is False
    assert result["output"]["review_blocked"] is True
    assert "tests_passed" not in result["output"] or result["output"]["tests_passed"] is not True
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_plan_only_no_edit_cannot_pass(tmp_path):
    """Plan response with zero file edits must never PASS or commit."""
    repo = green_repo(tmp_path, "repo_planonly")
    baseline = git(repo, "rev-parse", "HEAD")

    def fake_plan(goal, worktree, lessons="", intelligence=None):
        return {"goal": goal, "edits": []}

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(worker_module, "generate_edit_plan", fake_plan)
    try:
        result = RepoCodeWorker().execute(
            "Plan only, no edits.",
            {"repo_path": str(repo), "branch_name": "yodaw/plan-only"},
        )
    finally:
        monkeypatch.undo()

    assert result["success"] is False
    assert result["error"]["type"] in ("InvalidEditPlan", "NoChange")
    assert git(repo, "rev-parse", "HEAD") == baseline