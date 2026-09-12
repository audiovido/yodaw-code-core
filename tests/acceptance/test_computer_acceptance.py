"""Black-box computer acceptance.

Covers only what this base demonstrably supports:
worker filesystem evidence (workspace, git diff, pytest runs).
Every claim is derived from a real repository fixture and real
git/pytest execution. Native computer-control beyond this is
marked PENDING_WORKER_N.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission

client = TestClient(app)


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
    git(repo, "config", "user.email", "acceptance@test.local")
    git(repo, "config", "user.name", "YODAW Acceptance")

    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )
    (repo / "test_greet.py").write_text(
        "from greet import greet\n\n"
        "def test_greet():\n"
        "    assert greet('Armin') == 'Hello Armin'\n"
    )
    # Minimum test configuration recognized by
    # RepoCodeWorker.detect_test_commands().
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return repo, td


def _evidence_text(evidence) -> str:
    return " ".join(str(item) for item in evidence)


def _submit_edit_mission(goal: str, repo: Path) -> dict:
    return client.post(
        "/api/v1/missions",
        json={
            "goal": goal,
            "capability": "code",
            "metadata": {
                "repo_path": str(repo),
                "target_file": "greet.py",
                "find": "return 'Hello ' + name",
                "replace": "return f'Hello {name}'",
                "commit_message": "refactor greeting to f-string",
            },
        },
    ).json()


def test_code_worker_leaves_filesystem_evidence():
    repo, td = create_fixture_repo()
    try:
        queued = _submit_edit_mission(
            "Refactor greeting to use an f-string", repo
        )
        mission = poll_mission(client, queued["id"], timeout=60.0)

        assert mission["status"] == "PASS"

        blob = _evidence_text(mission["evidence"])
        assert "pytest" in blob
        assert "git" in blob

        # A real pytest command was executed, not a fabricated string.
        pytest_entries = [
            item
            for item in mission["evidence"]
            if isinstance(item, dict)
            and "pytest" in (item.get("cmd") or "")
        ]
        assert pytest_entries, "no real pytest command evidence"
        assert pytest_entries[0]["returncode"] == 0

        result = mission["result"]
        assert result["tests_passed"] is True
        assert re.fullmatch(r"[0-9a-f]{40}", result["commit_sha"])

        commit = result["commit_sha"]
        git(repo, "cat-file", "-e", f"{commit}^{{commit}}")
        assert "f'Hello {name}'" in git(repo, "show", f"{commit}:greet.py")
    finally:
        td.cleanup()


def test_code_worker_reports_workspace_and_commit():
    repo, td = create_fixture_repo()
    try:
        queued = _submit_edit_mission(
            "Refactor greeting to use an f-string", repo
        )
        mission = poll_mission(client, queued["id"], timeout=60.0)

        assert mission["status"] == "PASS"

        result = mission["result"]
        # Real values from execution, never fabricated.
        assert result["worktree"]
        assert re.fullmatch(r"[0-9a-f]{40}", result["commit_sha"])
        assert result["tests_passed"] is True

        commit = result["commit_sha"]
        git(repo, "cat-file", "-e", f"{commit}^{{commit}}")

        # Final worktree is actually clean.
        assert git(repo, "status", "--porcelain") == ""
    finally:
        td.cleanup()