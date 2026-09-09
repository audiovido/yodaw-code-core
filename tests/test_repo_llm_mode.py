import subprocess
import tempfile
from pathlib import Path

import app.workers.repo_code_worker as worker_module
from app.workers.repo_code_worker import RepoCodeWorker


def run(cmd, cwd):
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_repo_worker_can_use_generated_plan(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()

        run(["git", "init"], repo)
        run(["git", "config", "user.email", "test@example.com"], repo)
        run(["git", "config", "user.name", "Test"], repo)

        (repo / "calc.py").write_text(
            """def value():
    return 1
"""
        )

        (repo / "test_calc.py").write_text(
            """from calc import value

def test_value():
    assert value() == 2
"""
        )

        (repo / "pytest.ini").write_text(
            """[pytest]
pythonpath = .
"""
        )

        run(["git", "add", "."], repo)
        run(["git", "commit", "-m", "baseline"], repo)

        def fake_generate(goal, worktree, lessons=""):
            return {
                "action": "edit",
                "target_file": "calc.py",
                "find": "return 1",
                "replace": "return 2",
                "reason": "make test pass",
            }

        monkeypatch.setattr(
            worker_module,
            "generate_edit_plan",
            fake_generate,
        )

        worker = RepoCodeWorker()

        result = worker.execute(
            "Fix value function so tests pass",
            {
                "repo_path": str(repo),
                "commit_message": "fix value function",
            },
        )

        assert result["success"] is True, result
        assert result["output"]["tests_passed"] is True
        assert result["output"]["commit_sha"]
        assert result["output"]["working_tree_clean"] is True
