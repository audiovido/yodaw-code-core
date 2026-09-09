import os
import subprocess
import tempfile
from pathlib import Path

from app.workers.repo_code_worker import RepoCodeWorker


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def test_repo_worker_uses_isolated_worktree():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()

        run(["git", "init"], repo)
        run(["git", "config", "user.email", "test@example.com"], repo)
        run(["git", "config", "user.name", "Test"], repo)

        (repo / "sample.py").write_text("VALUE = 1\n")

        run(["git", "add", "."], repo)
        run(["git", "commit", "-m", "baseline"], repo)

        os.environ["YODAW_TARGET_REPO"] = str(repo)

        worker = RepoCodeWorker()
        result = worker.execute("Inspect repository safely")

        assert result["success"] is True
        assert result["output"]["repo"] == str(repo.resolve())
        assert result["output"]["branch"].startswith("yodaw/task-")
        assert result["output"]["working_tree_clean"] is True
        assert len(result["evidence"]) >= 5
