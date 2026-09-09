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


def init_repo(repo: Path, files: dict):
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    for name, content in files.items():
        (repo / name).write_bytes(content.encode())

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return git(repo, "rev-parse", "HEAD")


def test_successful_mission_removes_worktree(tmp_path):
    repo = tmp_path / "repo_success"

    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 2\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    assert result["success"] is True, result

    worktree = Path(result["output"]["worktree"])

    assert not worktree.exists(), (
        "successful missions must remove their worktree"
    )

    cleanup_events = [
        item
        for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "worktree_cleanup"
    ]

    assert cleanup_events
    assert cleanup_events[0]["action"] == "removed"

    # Source repository remains fully intact.
    assert (repo / "calc.py").read_text() == "def value():\n    return 1\n"


def test_failed_mission_keeps_worktree_for_debugging(tmp_path):
    repo = tmp_path / "repo_failed"

    init_repo(
        repo,
        {
            "a.py": "VALUE = 1\n",
            "b.py": "VALUE = 10\n",
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    baseline = git(repo, "rev-parse", "HEAD")

    result = RepoCodeWorker().execute(
        "Atomic failure test.",
        {
            "repo_path": str(repo),
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

    worktree = Path(result["output"]["worktree"])

    cleanup_events = [
        item
        for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "worktree_cleanup"
    ]

    assert cleanup_events
    assert cleanup_events[0]["action"] == "kept_failed_for_debugging"

    # Worktree kept for debugging: restored to baseline, clean.
    assert worktree.exists()
    assert worker_module.worktree_is_clean(worktree) is True
    assert (worktree / "a.py").read_text() == "VALUE = 1\n"
    assert (worktree / "b.py").read_text() == "VALUE = 10\n"

    # Source repository untouched.
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_keep_worktree_mode_preserves_successful_worktree(tmp_path):
    repo = tmp_path / "repo_keep"

    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 2\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "keep_worktree": True,
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    assert result["success"] is True, result

    worktree = Path(result["output"]["worktree"])

    assert worktree.exists(), "keep_worktree must preserve the worktree"

    cleanup_events = [
        item
        for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "worktree_cleanup"
    ]

    assert cleanup_events
    assert cleanup_events[0]["action"] == "kept_by_request"


def test_cleanup_failure_does_not_hide_mission_success(tmp_path, monkeypatch):
    repo = tmp_path / "repo_cleanup_fail"

    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 2\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    real_run = worker_module.run

    def failing_git_remove(cmd, cwd=None, timeout=300):
        if "worktree" in cmd and "remove" in cmd:
            return {
                "cmd": " ".join(cmd),
                "cwd": str(cwd) if cwd else None,
                "stdout": "",
                "stderr": "simulated git worktree remove failure",
                "returncode": 1,
                "timestamp": "simulated",
            }

        return real_run(cmd, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(worker_module, "run", failing_git_remove)

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    # The mission result must stand regardless of cleanup trouble.
    assert result["success"] is True, result

    cleanup_errors = [
        item
        for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "worktree_cleanup_error"
    ]

    assert cleanup_errors

    # rmtree fallback still ran; nothing half-removed remains.
    assert not Path(result["output"]["worktree"]).exists()


def test_crlf_target_file_is_edited_without_line_ending_rewrite(tmp_path):
    repo = tmp_path / "repo_crlf"

    init_repo(
        repo,
        {
            "calc.py": "def value():\r\n    return 1\r\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 2\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    assert result["success"] is True, result

    branch = result["output"]["branch"]

    # Byte-exact blob read: avoid Python text-mode newline
    # translation inside the test helper itself.
    raw = subprocess.run(
        ["git", "show", f"{branch}:calc.py"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout.decode()

    # The committed blob must keep CRLF endings and not introduce
    # a whole-file rewrite or LF contamination.
    assert "return 2\r\n" in raw
    assert "def value():\r\n" in raw
    assert "\r\n" in raw


def test_missing_npm_reports_clear_tool_error(tmp_path, monkeypatch):
    repo = tmp_path / "repo_npm"

    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "package.json": "{}\n",
        },
    )

    real_which = worker_module.which

    def no_npm(tool):
        if tool == "npm":
            return None

        return real_which(tool)

    monkeypatch.setattr(worker_module, "which", no_npm)

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "ToolMissingError"
    assert "npm" in result["error"]["message"].lower()


def test_missing_npm_reports_clear_tool_error_patched(
    tmp_path,
    monkeypatch,
):
    """
    Hermetic variant: force npm missing via monkeypatched which()
    regardless of the host environment.
    """
    repo = tmp_path / "repo_npm2"

    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "package.json": "{}\n",
        },
    )

    real_which = worker_module.which

    def no_npm(tool):
        if tool == "npm":
            return None

        return real_which(tool)

    monkeypatch.setattr(worker_module, "which", no_npm)

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "ToolMissingError"
    assert "npm" in result["error"]["message"].lower()
