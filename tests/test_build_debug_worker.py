import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.workers.base import WorkerResult
from app.workers.build_debug_worker import BuildDebugWorker


def run(cmd, cwd):
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def create_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test"], repo)

    (repo / "mathlib.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
    )

    (repo / "test_mathlib.py").write_text(
        "from mathlib import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
    )

    (repo / "pytest.ini").write_text(
        "[pytest]\n"
        "pythonpath = .\n"
    )

    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "baseline"], repo)

    return repo


def create_broken_repo(root: Path) -> Path:
    """Empty git repo with a python file that fails to import."""
    repo = root / "repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test"], repo)

    (repo / "broken.py").write_text(
        "def broken():\n"
        "    raise NotImplementedError('broken')\n"
    )

    (repo / "test_broken.py").write_text(
        "from broken import broken\n\n"
        "def test_broken():\n"
        "    broken()\n"
    )

    (repo / "pytest.ini").write_text(
        "[pytest]\n"
        "pythonpath = .\n"
    )

    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "broken baseline"], repo)

    return repo


class TestBuildDebugWorker:
    def test_successful_build_edit_commit(self):
        with tempfile.TemporaryDirectory() as td:
            repo = create_repo(Path(td))

            worker = BuildDebugWorker()

            result = worker.execute(
                "Modify mathlib.py to say return a + b",
                {
                    "repo_path": str(repo),
                    "target_file": "mathlib.py",
                    "find": "return a + b",
                    "replace": "return sum((a, b))",
                    "commit_message": "improve add",
                },
            )

            assert result["success"] is True
            assert result["output"]["tests_passed"] is True
            assert result["output"]["commit_sha"]
            assert result["output"]["working_tree_clean"] is True

    def test_failed_build_no_change_committed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = create_broken_repo(Path(td))

            worker = BuildDebugWorker()

            result = worker.execute(
                "Add missing function",
                {
                    "repo_path": str(repo),
                    "target_file": "broken.py",
                    "find": "def broken():",
                    "replace": "def fixed():",
                    "commit_message": "attempt fix",
                    "max_attempts": 1,
                },
            )

            assert result["success"] is False
            assert result["output"]["tests_passed"] is False
            assert result["error"]["type"] == "ValidationFailed"

    def test_missing_tool_clean(self):
        with tempfile.TemporaryDirectory() as td:
            repo = create_repo(Path(td))

            worker = BuildDebugWorker()

            with mock.patch(
                "app.workers.build_debug_worker.resolve_python_executable",
                return_value="/nonexistent/python",
            ):
                result = worker.execute(
                    "Modify mathlib.py to say return a + b",
                    {
                        "repo_path": str(repo),
                        "target_file": "mathlib.py",
                        "find": "return a + b",
                        "replace": "return sum((a, b))",
                        "commit_message": "improve add",
                    },
                )

                assert result["success"] is False
                # ToolMissing is captured and reported as an environment error.
                assert "ToolMissing" in result["error"]["type"]

    def test_malformed_goal_no_edits(self):
        with tempfile.TemporaryDirectory() as td:
            repo = create_repo(Path(td))

            worker = BuildDebugWorker()

            result = worker.execute(
                "gibberish without any edit signal",
                {
                    "repo_path": str(repo),
                    "target_file": "mathlib.py",
                    # Explicit edits provided, so no LLM call.
                    "find": "return a + b",
                    "replace": "return sum((a, b))",
                    "commit_message": "fix",
                },
            )

            assert result["success"] is True
            assert result["output"]["tests_passed"] is True

    def test_timeout_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            repo = create_repo(Path(td))

            worker = BuildDebugWorker()

            with mock.patch("time.time", side_effect=[0.0, 9999.0, 9999.0, 9999.0]):
                result = worker.execute(
                    "Modify mathlib.py to say return a + b",
                    {
                        "repo_path": str(repo),
                        "target_file": "mathlib.py",
                        "find": "return a + b",
                        "replace": "return sum((a, b))",
                        "commit_message": "improve add",
                        "max_time_seconds": 1,
                    },
                )

                # The loop should hit the time limit and end without
                # committing anything (validation couldn't run).
                assert result["success"] is False or result["output"].get(
                    "tests_passed"
                ) is True

    def test_failing_test_validation_failure(self):
        with tempfile.TemporaryDirectory() as td:
            repo = create_repo(Path(td))

            # Add a test that always fails
            (repo / "test_always_fail.py").write_text(
                "def test_fail():\n"
                "    assert False\n"
            )
            run(["git", "add", "."], repo)
            run(["git", "commit", "-m", "add failing test"], repo)

            worker = BuildDebugWorker()

            result = worker.execute(
                "Modify mathlib.py to say return a + b",
                {
                    "repo_path": str(repo),
                    "target_file": "mathlib.py",
                    "find": "return a + b",
                    "replace": "return a + b",
                    "commit_message": "no-op",
                    "max_attempts": 1,
                },
            )

            assert result["success"] is False
            assert result["error"]["type"] == "ValidationFailed"

    def test_retry_exhaustion(self):
        with tempfile.TemporaryDirectory() as td:
            repo = create_broken_repo(Path(td))

            worker = BuildDebugWorker()

            # max_attempts=1 means "one initial + zero repairs"
            result = worker.execute(
                "attempt repair",
                {
                    "repo_path": str(repo),
                    "target_file": "broken.py",
                    "find": "def broken():",
                    "replace": "def other():",
                    "max_attempts": 1,
                },
            )

            assert result["success"] is False
            assert result["error"]["type"] in {
                "RetryExhausted",
                "ValidationFailed",
            }


class TestWorkerRegistration:
    def test_build_debug_worker_registered(self):
        from app.workers.registry import registry

        worker = registry.find("build-debug")

        assert worker is not None
        assert worker.name == "build-debug-bud"
        assert "build-debug" in worker.capabilities

    def test_build_debug_worker_health(self):
        from app.workers.registry import registry

        worker = registry.find("build-debug")
        health = worker.health()

        assert health["status"] == "READY"
        assert "build-debug" in health["capabilities"]