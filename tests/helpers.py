"""
Shared Stage 8 test helpers.

The Stage 8 API contract is asynchronous: POST returns QUEUED
immediately and execution completes independently. Tests that
verify end outcomes poll the mission with a deadline instead of
asserting on the POST response body.
"""

import subprocess
import time
from typing import Optional

from fastapi.testclient import TestClient

TERMINAL = {"PASS", "FAIL", "BLOCKED", "CANCELLED"}


def poll_mission(
    client: TestClient,
    mission_id: str,
    timeout: float = 30.0,
    interval: float = 0.1,
    headers: Optional[dict] = None,
):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/missions/{mission_id}", headers=headers
        )
        assert response.status_code == 200

        mission = response.json()

        if mission["status"] in TERMINAL:
            return mission

        time.sleep(interval)

    raise AssertionError(
        f"mission {mission_id} did not reach a terminal state "
        f"within {timeout}s"
    )


def make_smoke_repo(root) -> str:
    """Real temporary git repo for a deterministic repo mission.

    The repo fails its test at baseline ('old') and the deterministic
    edit metadata in the caller (target_file/find/replace) makes the
    test pass, so missions reach PASS without any LLM provider.
    """
    repo = root / "smoke-repo"
    repo.mkdir()

    def run(*args):
        subprocess.run(
            ["git", *args], cwd=repo, check=True,
            capture_output=True, text=True,
        )

    run("init")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    (repo / "app.py").write_text("def greet():\n    return 'old'\n")
    (repo / "test_app.py").write_text(
        "from app import greet\n\n"
        "def test_greet():\n"
        "    assert greet() == 'hello'\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    run("add", ".")
    run("commit", "-m", "baseline")
    return str(repo)


def smoke_mission_payload(goal: str) -> dict:
    """Mission payload that executes a real deterministic repo edit."""
    return {
        "goal": goal,
        "capability": "code",
        "metadata": {
            "target_file": "app.py",
            "find": "return 'old'",
            "replace": "return 'hello'",
        },
    }
