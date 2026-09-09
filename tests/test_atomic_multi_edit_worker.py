import subprocess
from pathlib import Path

import app.workers.repo_code_worker as worker_module
from app.workers.repo_code_worker import RepoCodeWorker


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repo(repo, files):
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    for name, content in files.items():
        (repo / name).write_text(content)

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return git(repo, "rev-parse", "HEAD")


def test_atomic_multi_file_execution(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    (repo / "service.py").write_text(
        "def value():\n"
        "    return 1\n"
    )

    (repo / "helpers.py").write_text(
        "def helper():\n"
        "    return 10\n"
    )

    (repo / "test_app.py").write_text(
        "from service import value\n"
        "from helpers import helper\n\n"
        "def test_values():\n"
        "    assert value() == 2\n"
        "    assert helper() == 20\n"
    )

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    worker = RepoCodeWorker()

    result = worker.execute(
        "Update both values.",
        {
            "repo_path": str(repo),
            "branch_name": "yodaw/test-multi-edit",
            "commit_message": "update both values",
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
        },
    )

    assert result["success"] is True

    output = result["output"]

    assert output["tests_passed"] is True
    assert output["working_tree_clean"] is True
    assert output["edit_count"] == 2
    assert set(output["target_files"]) == {
        "service.py",
        "helpers.py",
    }

    # Successful missions remove the isolated worktree; the commit
    # is verified through git objects on the mission branch.
    branch = output["branch"]

    changed = set(
        git(
            repo,
            "show",
            "--name-only",
            "--pretty=format:",
            branch,
        ).splitlines()
    )

    assert changed == {
        "service.py",
        "helpers.py",
    }


def test_invalid_second_edit_does_not_apply_first(tmp_path):
    repo = tmp_path / "repo_fail"
    repo.mkdir()

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    (repo / "a.py").write_text("VALUE = 1\n")
    (repo / "b.py").write_text("VALUE = 10\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    baseline = git(repo, "rev-parse", "HEAD")

    worker = RepoCodeWorker()

    result = worker.execute(
        "Atomic failure test.",
        {
            "repo_path": str(repo),
            "branch_name": "yodaw/test-atomic-failure",
            "edits": [
                {
                    "target_file": "a.py",
                    "find": "VALUE = 1",
                    "replace": "VALUE = 2",
                },
                {
                    "target_file": "b.py",
                    "find": "THIS DOES NOT EXIST",
                    "replace": "VALUE = 20",
                },
            ],
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "FindTextMissing"

    worktree = result["output"].get("worktree")

    # The source repository itself must remain untouched.
    assert git(repo, "rev-parse", "HEAD") == baseline
    assert (repo / "a.py").read_text() == "VALUE = 1\n"
    assert (repo / "b.py").read_text() == "VALUE = 10\n"


def test_legacy_llm_plan_is_normalized_to_single_edit(tmp_path, monkeypatch):
    repo = tmp_path / "repo_legacy"
    init_repo(
        repo,
        {
            "service.py": "def value():\n    return 1\n",
            "test_service.py": (
                "from service import value\n\n\n"
                "def test_value():\n    assert value() == 2\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    def fake_generate(goal, worktree, lessons=""):
        # Legacy Stage 6 single-edit plan shape.
        return {
            "action": "edit",
            "target_file": "service.py",
            "find": "return 1",
            "replace": "return 2",
            "reason": "legacy plan shape",
        }

    monkeypatch.setattr(worker_module, "generate_edit_plan", fake_generate)

    result = RepoCodeWorker().execute(
        "Bump value.",
        {"repo_path": str(repo)},
    )

    assert result["success"] is True, result

    output = result["output"]

    assert output["edit_count"] == 1
    assert output["target_files"] == ["service.py"]
    assert output["tests_passed"] is True
    assert output["working_tree_clean"] is True


def test_path_escape_is_rejected_before_any_write(tmp_path):
    repo = tmp_path / "repo_escape"
    init_repo(
        repo,
        {
            "a.py": "VALUE = 1\n",
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    escaped = Path("workspace") / "outside_escape_probe.txt"

    try:
        result = RepoCodeWorker().execute(
            "Escape attempt.",
            {
                "repo_path": str(repo),
                "edits": [
                    {
                        "target_file": "a.py",
                        "find": "VALUE = 1",
                        "replace": "VALUE = 2",
                    },
                    {
                        "target_file": "../outside_escape_probe.txt",
                        "find": "untouched",
                        "replace": "hacked",
                    },
                ],
            },
        )

        assert result["success"] is False
        assert result["error"]["type"] == "PathEscapeError"
        assert not escaped.exists()
        assert (repo / "a.py").read_text() == "VALUE = 1\n"

    finally:
        if escaped.exists():
            escaped.unlink()


def test_validation_failure_restores_all_files_and_commits_nothing(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo_validation"
    init_repo(
        repo,
        {
            "a.py": "VALUE = 1\n",
            "b.py": "VALUE = 10\n",
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    baseline = git(repo, "rev-parse", "HEAD")

    real_run = worker_module.run

    def fail_test_commands(cmd, cwd=None, timeout=300):
        if cmd and cmd[0] == "python" and "pytest" in cmd:
            return {
                "cmd": " ".join(cmd),
                "cwd": str(cwd) if cwd else None,
                "stdout": "",
                "stderr": "simulated test failure",
                "returncode": 1,
                "timestamp": "simulated",
            }

        return real_run(cmd, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(worker_module, "run", fail_test_commands)

    result = RepoCodeWorker().execute(
        "Should fail validation.",
        {
            "repo_path": str(repo),
            "max_retries": 0,
            "edits": [
                {
                    "target_file": "a.py",
                    "find": "VALUE = 1",
                    "replace": "VALUE = 2",
                },
                {
                    "target_file": "b.py",
                    "find": "VALUE = 10",
                    "replace": "VALUE = 20",
                },
            ],
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "ValidationFailed"

    recovery_events = [
        item
        for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "recovery"
    ]

    assert recovery_events
    assert recovery_events[0]["action"] == "revert_failed_edits"
    assert set(recovery_events[0]["files"]) == {"a.py", "b.py"}

    # Every edited file must be restored inside the worktree.
    worktree = Path(result["output"]["worktree"])
    assert (worktree / "a.py").read_text() == "VALUE = 1\n"
    assert (worktree / "b.py").read_text() == "VALUE = 10\n"

    # No commit may exist beyond the baseline commit.
    assert git(worktree, "rev-list", "--count", "HEAD") == "1"

    # The source repository must never be mutated directly.
    assert git(repo, "rev-parse", "HEAD") == baseline
    assert (repo / "a.py").read_text() == "VALUE = 1\n"


def test_repeated_edits_to_same_file_compose_in_memory(tmp_path):
    repo = tmp_path / "repo_same_file"
    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 42\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    result = RepoCodeWorker().execute(
        "Rewrite value through staged steps.",
        {
            "repo_path": str(repo),
            "commit_message": "compose staged edits",
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 40 + 1",
                },
                {
                    "target_file": "calc.py",
                    "find": "return 40 + 1",
                    "replace": "return 42",
                },
            ],
        },
    )

    assert result["success"] is True, result

    output = result["output"]

    assert output["edit_count"] == 2
    assert output["target_files"] == ["calc.py"]
    assert output["working_tree_clean"] is True

    # The second edit matched text that only existed in the
    # staged in-memory content of the first edit; the final
    # content is verified through the committed git object.
    branch = output["branch"]

    final_text = git(repo, "show", f"{branch}:calc.py")

    # git() strips trailing whitespace, so compare without the
    # final newline of the stored blob.
    assert final_text == "def value():\n    return 42"
