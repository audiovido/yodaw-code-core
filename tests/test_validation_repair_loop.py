import subprocess
from pathlib import Path

import pytest

import app.workers.repo_code_worker as worker_module
from app.llm.provider import LLMError
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


BROKEN_DIVIDE_PLAN = {
    "action": "edit",
    "edits": [
        {
            "target_file": "calculator.py",
            "find": "return a / b",
            "replace": "return a // b",
        }
    ],
    "reason": "intentionally broken first attempt",
}


@pytest.fixture
def divide_repo(tmp_path):
    repo = tmp_path / "repo"

    init_repo(
        repo,
        {
            "calculator.py": (
                "def divide(a, b):\n"
                "    return a / b\n"
            ),
            "test_calculator.py": (
                "from calculator import divide\n\n\n"
                "def test_divide():\n"
                "    assert divide(6, 3) == 2\n"
                "    assert divide(7, 2) == 3.5\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    return repo


def make_llm(monkeypatch, plans):
    """
    Fake Coder Brain. `plans` is a list of (func, plan) pairs.
    Plans are returned in order: initial plan first, then one
    repair plan per validation failure.
    """
    calls = {"plan": 0, "repair": 0}
    prompts = []

    def fake_generate_edit_plan(goal, worktree, lessons=""):
        func, plan = plans[calls["plan"]]
        calls["plan"] += 1
        prompts.append(("plan", func, goal))
        return plan

    def fake_generate_repair_plan(
        goal,
        worktree,
        previous_plan,
        failure_context,
        provider=None,
        lessons="",
    ):
        func, plan = plans[1 + calls["repair"]]
        calls["repair"] += 1
        prompts.append(("repair", func, goal))
        return plan

    monkeypatch.setattr(
        worker_module,
        "generate_edit_plan",
        fake_generate_edit_plan,
    )
    monkeypatch.setattr(
        worker_module,
        "generate_repair_plan",
        fake_generate_repair_plan,
    )

    return calls, prompts


# The repair plan must target the RESTORED baseline content:
# failed attempts are reverted before the repair planner runs.
GOOD_REPAIR_PLAN = {
    "action": "edit",
    "edits": [
        {
            "target_file": "calculator.py",
            "find": "return a / b",
            "replace": "return a / b if b != 0 else 0",
        }
    ],
    "reason": "restore true division with zero-division guard",
}


# ---------------------------------------------------------------
# A. First attempt fails, repair attempt passes.
# ---------------------------------------------------------------
def test_first_attempt_fails_second_passes(divide_repo, monkeypatch):
    baseline = git(divide_repo, "rev-parse", "HEAD")

    calls, prompts = make_llm(
        monkeypatch,
        [
            ("initial", BROKEN_DIVIDE_PLAN),
            ("repair", GOOD_REPAIR_PLAN),
        ],
    )

    result = RepoCodeWorker().execute(
        "Keep true division working.",
        {"repo_path": str(divide_repo)},
    )

    assert result["success"] is True, result
    assert calls == {"plan": 1, "repair": 1}

    output = result["output"]

    assert output["tests_passed"] is True
    assert output["retries"] == 1
    assert output["attempts"] == 2
    assert output["working_tree_clean"] is True

    # Successful missions remove the isolated worktree; the
    # final state is verified through git objects on the branch.
    branch = output["branch"]

    # Exactly one commit beyond baseline and only final code in it.
    assert git(divide_repo, "rev-list", "--count", branch) == "2"
    final_code = git(
        divide_repo,
        "show",
        f"{branch}:calculator.py",
    )
    assert "return a / b" in final_code
    assert "a // b" not in final_code

    # The broken attempt must never have been committed.
    commit_files = git(
        divide_repo,
        "show",
        "--name-only",
        "--pretty=format:",
        branch,
    ).split()
    assert commit_files == ["calculator.py"]

    # Source repository untouched until commit inside worktree.
    assert git(divide_repo, "rev-parse", "HEAD") == baseline

    # Evidence must record the failure, the repair plan, and
    # the recovery between attempts.
    evidence_types = [
        item.get("type") for item in result["evidence"] if isinstance(item, dict)
    ]

    assert "validation_failure" in evidence_types
    assert "repair_plan" in evidence_types
    assert "recovery" in evidence_types


# ---------------------------------------------------------------
# B. Retry exhaustion: every attempt fails.
# ---------------------------------------------------------------
def test_retry_exhaustion_returns_failure_without_commit(
    divide_repo,
    monkeypatch,
):
    baseline = git(divide_repo, "rev-parse", "HEAD")

    calls, prompts = make_llm(
        monkeypatch,
        [
            ("initial", BROKEN_DIVIDE_PLAN),
            ("repair", BROKEN_DIVIDE_PLAN),
        ],
    )

    result = RepoCodeWorker().execute(
        "Keep true division working.",
        {
            "repo_path": str(divide_repo),
            "max_retries": 1,
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "RetryExhausted"

    assert calls == {"plan": 1, "repair": 1}

    output = result["output"]
    assert output["tests_passed"] is False
    assert output["retries"] == 1
    assert output["attempts"] == 2

    worktree = Path(output["worktree"])

    # Zero commits beyond baseline.
    assert git(worktree, "rev-list", "--count", "HEAD") == "1"

    # Worktree restored to the attempt baseline.
    assert (worktree / "calculator.py").read_text() == (
        "def divide(a, b):\n    return a / b\n"
    )
    assert worker_module.worktree_is_clean(worktree) is True

    # Both attempts recorded in evidence.
    failures = [
        item
        for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "validation_failure"
    ]

    assert len(failures) == 2
    assert failures[0]["attempt"] == 0
    assert failures[1]["attempt"] == 1

    # Source repository never mutated.
    assert git(divide_repo, "rev-parse", "HEAD") == baseline
    assert (divide_repo / "calculator.py").read_text() == (
        "def divide(a, b):\n    return a / b\n"
    )


# ---------------------------------------------------------------
# C. Repair planner returns blocked.
# ---------------------------------------------------------------
def test_repair_plan_blocked_returns_clean_failure(divide_repo, monkeypatch):
    baseline = git(divide_repo, "rev-parse", "HEAD")

    make_llm(
        monkeypatch,
        [
            ("initial", BROKEN_DIVIDE_PLAN),
            (
                "repair",
                {"action": "blocked", "reason": "not enough context"},
            ),
        ],
    )

    result = RepoCodeWorker().execute(
        "Keep true division working.",
        {"repo_path": str(divide_repo)},
    )

    assert result["success"] is False
    assert result["error"]["type"] == "LLMBlocked"
    assert "not enough context" in result["error"]["message"]

    worktree = Path(result["output"]["worktree"])

    # Zero commits beyond baseline and restored worktree.
    assert git(worktree, "rev-list", "--count", "HEAD") == "1"
    assert (worktree / "calculator.py").read_text() == (
        "def divide(a, b):\n    return a / b\n"
    )
    assert worker_module.worktree_is_clean(worktree) is True

    # Failure evidence and repair plan evidence are preserved.
    evidence_types = [
        item.get("type") for item in result["evidence"] if isinstance(item, dict)
    ]

    assert "validation_failure" in evidence_types
    assert "repair_plan" in evidence_types

    assert git(divide_repo, "rev-parse", "HEAD") == baseline


# ---------------------------------------------------------------
# D. Repair planner raises LLMError.
# ---------------------------------------------------------------
def test_repair_llm_error_preserves_evidence(divide_repo, monkeypatch):
    baseline = git(divide_repo, "rev-parse", "HEAD")

    def fake_generate_edit_plan(goal, worktree, lessons=""):
        return BROKEN_DIVIDE_PLAN

    def fake_generate_repair_plan(*args, **kwargs):
        raise LLMError("simulated provider outage")

    monkeypatch.setattr(
        worker_module,
        "generate_edit_plan",
        fake_generate_edit_plan,
    )
    monkeypatch.setattr(
        worker_module,
        "generate_repair_plan",
        fake_generate_repair_plan,
    )

    result = RepoCodeWorker().execute(
        "Keep true division working.",
        {"repo_path": str(divide_repo)},
    )

    assert result["success"] is False
    assert result["error"]["type"] == "LLMError"
    assert "simulated provider outage" in result["error"]["message"]

    output = result["output"]

    assert output["tests_passed"] is False
    assert output["retries"] == 1
    assert output["attempts"] == 2

    worktree = Path(output["worktree"])

    # Zero commits and restored worktree.
    assert git(worktree, "rev-list", "--count", "HEAD") == "1"
    assert (worktree / "calculator.py").read_text() == (
        "def divide(a, b):\n    return a / b\n"
    )
    assert worker_module.worktree_is_clean(worktree) is True

    # Failure evidence preserved before the provider failure.
    evidence_types = [
        item.get("type") for item in result["evidence"] if isinstance(item, dict)
    ]

    assert "validation_failure" in evidence_types

    assert git(divide_repo, "rev-parse", "HEAD") == baseline


# ---------------------------------------------------------------
# E. Multi-file failed attempt, multi-file repair.
# ---------------------------------------------------------------
def test_multi_file_failed_attempt_then_multi_file_repair(tmp_path, monkeypatch):
    repo = tmp_path / "repo_multi"

    init_repo(
        repo,
        {
            "service.py": "def value():\n    return 1\n",
            "helpers.py": "def helper():\n    return 10\n",
            "test_app.py": (
                "from service import value\n"
                "from helpers import helper\n\n\n"
                "def test_values():\n"
                "    assert value() == 2\n"
                "    assert helper() == 20\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    baseline = git(repo, "rev-parse", "HEAD")

    broken_plan = {
        "action": "edit",
        "edits": [
            {
                "target_file": "service.py",
                "find": "return 1",
                "replace": "return 3",
            },
            {
                "target_file": "helpers.py",
                "find": "return 10",
                "replace": "return 30",
            },
        ],
        "reason": "broken first attempt",
    }

    repair_plan = {
        "action": "edit",
        "edits": [
            {
                "target_file": "service.py",
                "find": "return 1",
                "replace": "return 2",
            },
            {
                "target_file": "helpers.py",
                "find": "return 10",
                "replace": "return 20",
            },
        ],
        "reason": "correct both values",
    }

    calls, prompts = make_llm(
        monkeypatch,
        [
            ("initial", broken_plan),
            ("repair", repair_plan),
        ],
    )

    result = RepoCodeWorker().execute(
        "Update both values.",
        {"repo_path": str(repo)},
    )

    assert result["success"] is True, result

    output = result["output"]

    assert output["retries"] == 1
    assert output["attempts"] == 2
    assert output["edit_count"] == 2
    assert set(output["target_files"]) == {"service.py", "helpers.py"}
    assert output["working_tree_clean"] is True

    branch = output["branch"]

    # Atomic behavior across attempts: exactly one commit with
    # only the corrected content, nothing from the failed attempt.
    assert git(repo, "rev-list", "--count", branch) == "2"

    changed = set(
        git(
            repo,
            "show",
            "--name-only",
            "--pretty=format:",
            branch,
        ).split()
    )

    assert changed == {"service.py", "helpers.py"}

    service_code = git(repo, "show", f"{branch}:service.py")
    helpers_code = git(repo, "show", f"{branch}:helpers.py")

    assert "return 2" in service_code
    assert "return 3" not in service_code
    assert "return 20" in helpers_code
    assert "return 30" not in helpers_code

    assert git(repo, "rev-parse", "HEAD") == baseline


# ---------------------------------------------------------------
# F. No retry when the first attempt passes.
# ---------------------------------------------------------------
def test_no_repair_when_first_attempt_passes(divide_repo, monkeypatch):
    good_initial_plan = {
        "action": "edit",
        "edits": [
            {
                "target_file": "calculator.py",
                "find": "return a / b",
                "replace": "return a / b if b else 0",
            }
        ],
        "reason": "initial plan already correct",
    }

    def failing_repair(*args, **kwargs):
        raise AssertionError("repair planner must not be called")

    monkeypatch.setattr(
        worker_module,
        "generate_edit_plan",
        lambda goal, worktree, lessons="": good_initial_plan,
    )
    monkeypatch.setattr(
        worker_module,
        "generate_repair_plan",
        failing_repair,
    )

    result = RepoCodeWorker().execute(
        "Keep true division working.",
        {"repo_path": str(divide_repo)},
    )

    assert result["success"] is True, result

    output = result["output"]

    assert output["retries"] == 0
    assert output["attempts"] == 1
    assert output["tests_passed"] is True
    assert output["working_tree_clean"] is True

    # One final commit containing the initial plan only; the
    # worktree itself is removed by the success lifecycle policy.
    assert git(
        divide_repo,
        "rev-list",
        "--count",
        output["branch"],
    ) == "2"
