"""Black-box computer acceptance.

Covers only what this base demonstrably supports:
worker filesystem evidence (worktree, git diff, pytest runs).
Native computer-control beyond this is marked PENDING_WORKER_N.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission

def set_up_git_repo(temp_dir):
    repo_path = Path(temp_dir)
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True)
    # Create a test file
    test_file = repo_path / "test_calculator.py"
    test_file.write_text("""def add(a, b):
    return a + b


def subtract(a, b):
    return a - b
""")
    # Create a test file for pytest
    test_test_file = repo_path / "test_test_calculator.py"
    test_test_file.write_text("""import sys
sys.path.insert(0, '.')

from test_calculator import add, subtract


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
    assert add(0, 0) == 0

def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(0, 4) == -4
    assert subtract(3, 3) == 0
""")
    # Create pytest configuration
    pytest_ini = repo_path / "pytest.ini"
    pytest_ini.write_text("""[pytest]
testpaths = .
python_files = test_*.py
python_functions = test_*
""")
    # Initial commit
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit with test"], cwd=repo_path, check=True, capture_output=True)
    return str(repo_path)

client = TestClient(app)

def _evidence_text(evidence) -> str:
    return " ".join(str(item) for item in evidence)


def _mission_payload(repo_path: str, goal: str) -> dict:
    """Mission body the code worker can execute without an LLM:
    explicit edits + target repo, so evidence is real git/pytest output."""
    original = (Path(repo_path) / "test_calculator.py").read_text()
    replacement = original + """\n
def multiply(a, b):
    return a * b
"""
    return {
        "goal": goal,
        "capability": "code",
        "repo_path": repo_path,
        "metadata": {
            "edits": [{
                "target_file": "test_calculator.py",
                "find": original,
                "replace": replacement,
            }]
        },
    }


def test_code_worker_leaves_filesystem_evidence():
    # Repo worker leaves real filesystem evidence: pytest output and git
    # commands from the actual edit/validate/commit round-trip.
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = set_up_git_repo(temp_dir)
        queued = client.post(
            "/api/v1/missions",
            json=_mission_payload(repo_path, "Add multiply function to calculator"),
        ).json()
        mission = poll_mission(client, queued["id"], timeout=60.0)
        assert mission["status"] == "PASS"
        blob = _evidence_text(mission["evidence"])
        assert "pytest" in blob
        assert "git" in blob


def test_code_worker_reports_workspace_and_commit():
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = set_up_git_repo(temp_dir)
        queued = client.post(
            "/api/v1/missions",
            json=_mission_payload(repo_path, "Add multiply function to calculator"),
        ).json()
        mission = poll_mission(client, queued["id"], timeout=60.0)
        assert mission["status"] == "PASS"
        result = mission["result"]
        # Real repository identity: canonical target and worktree location
        assert Path(result["repo"]) == Path(repo_path).resolve()
        assert result["worktree"]
        assert result["commit_sha"]
        assert result["tests_passed"] is True