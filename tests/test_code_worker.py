import re
import subprocess
import tempfile
from pathlib import Path

from app.workers.code_worker import CodeWorker


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def create_fixture_repo() -> tuple[Path, tempfile.TemporaryDirectory]:
    """Real git repository with source, test config and baseline commit."""
    td = tempfile.TemporaryDirectory()
    repo = Path(td.name) / "repo"
    repo.mkdir()

    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")

    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )
    (repo / "test_greet.py").write_text(
        "from greet import greet\n\n"
        "def test_greet():\n"
        "    assert greet('Armin') == 'Hello Armin'\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return repo, td


def test_real_code_worker_end_to_end():
    repo, td = create_fixture_repo()
    try:
        worker = CodeWorker()
        result = worker.execute(
            "Refactor greeting to use an f-string",
            {
                "repo_path": str(repo),
                "target_file": "greet.py",
                "find": "return 'Hello ' + name",
                "replace": "return f'Hello {name}'",
                "commit_message": "refactor greeting to f-string",
            },
        )

        assert result["success"] is True

        output = result["output"]
        assert output["tests_passed"] is True
        assert re.fullmatch(r"[0-9a-f]{40}", output["commit_sha"])
        assert output["working_tree_clean"] is True
        assert output["worktree"]

        # Commit is real, not a fabricated string.
        commit = output["commit_sha"]
        git(repo, "cat-file", "-e", f"{commit}^{{commit}}")

        # Working tree is actually clean.
        assert git(repo, "status", "--porcelain") == ""

        # Evidence contains real pytest/git command runs.
        blob = " ".join(str(item) for item in result["evidence"])
        assert "pytest" in blob
        assert "git" in blob
    finally:
        td.cleanup()


def test_code_worker_no_repo_reports_planning_only():
    worker = CodeWorker()
    result = worker.execute("Plan something", None)

    assert result["success"] is True
    assert result["output"]["repo"]["observed"] is False

    # No fabricated repo-derived values when no repository target.
    assert "tests_passed" not in result["output"]
    assert "commit_sha" not in result["output"]
    assert "working_tree_clean" not in result["output"]
    assert "workspace" not in result["output"]
    blob = " ".join(str(item) for item in result["evidence"])
    assert "pytest" not in blob
    assert "git" not in blob