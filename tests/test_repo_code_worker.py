import os
import subprocess
import tempfile
from pathlib import Path

from app.workers.repo_code_worker import RepoCodeWorker


def run(cmd, cwd):
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def create_repo(root: Path):
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


def test_repo_worker_real_edit_commit():
    with tempfile.TemporaryDirectory() as td:
        repo = create_repo(Path(td))

        worker = RepoCodeWorker()

        result = worker.execute(
            "Improve add implementation",
            {
                "repo_path": str(repo),
                "target_file": "mathlib.py",
                "find": "return a + b",
                "replace": "return sum((a, b))",
                "commit_message": "improve add implementation",
            },
        )

        assert result["success"] is True
        assert result["output"]["tests_passed"] is True
        assert result["output"]["commit_sha"]
        assert result["output"]["working_tree_clean"] is True
        assert "sum((a, b))" in result["output"]["diff"]


def test_repo_worker_rejects_dirty_source_repo():
    with tempfile.TemporaryDirectory() as td:
        repo = create_repo(Path(td))

        (repo / "dirty.txt").write_text("dirty")

        worker = RepoCodeWorker()

        result = worker.execute(
            "Should not execute",
            {
                "repo_path": str(repo),
                "target_file": "mathlib.py",
                "find": "return a + b",
                "replace": "return sum((a, b))",
            },
        )

        assert result["success"] is False
        assert result["error"]["type"] == "DirtyRepo"
