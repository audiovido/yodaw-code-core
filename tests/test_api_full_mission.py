import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

import app.workers.repo_code_worker as worker_module
from app.main import app
from tests.helpers import poll_mission


client = TestClient(app)


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def create_fixture_repo():
    td = tempfile.TemporaryDirectory()

    repo = Path(td.name) / "repo"
    repo.mkdir()

    git(repo, "init")
    git(repo, "config", "user.email", "api@test.local")
    git(repo, "config", "user.name", "YODAW API Test")

    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )

    (repo / "test_greet.py").write_text(
        "from greet import greet\n\n\n"
        "def test_greet():\n    assert greet('Armin') == 'Hello Armin'\n"
    )

    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return repo, td


def test_full_api_mission_pipeline_hermetic(monkeypatch):
    """
    Canonical Stage 7.6 gate: POST /api/v1/missions -> orchestrator
    -> RepoCodeWorker -> plan -> worktree edit -> validation ->
    commit -> evidence -> learning record. The provider is faked so
    the test is hermetic; the full route is not.
    """
    repo, td = create_fixture_repo()

    try:
        baseline = git(repo, "rev-parse", "HEAD")

        fake_plan = {
            "action": "edit",
            "edits": [
                {
                    "target_file": "greet.py",
                    "find": "return 'Hello ' + name",
                    "replace": "return f'Hello {name}'",
                }
            ],
            "reason": "use f-string",
        }

        captured = {}

        def fake_plan_with_lessons(goal, worktree, lessons=""):
            captured["lessons"] = lessons

            return fake_plan

        monkeypatch.setattr(
            worker_module,
            "generate_edit_plan",
            fake_plan_with_lessons,
        )

        response = client.post(
            "/api/v1/missions",
            json={
                "goal": (
                    "Refactor greet(name) to use an f-string "
                    "without changing behavior."
                ),
                "capability": "repo-code",
                "metadata": {
                    "repo_path": str(repo),
                    "commit_message": "use f-string greeting",
                },
            },
        )

        assert response.status_code == 200, response.text

        # Stage 8 contract: POST returns QUEUED immediately;
        # execution completes asynchronously.
        queued = response.json()

        assert queued["status"] == "QUEUED"
        assert queued["id"]

        mission = poll_mission(client, queued["id"])

        # Mission ends PASS with the repo-code worker selected.
        assert mission["status"] == "PASS"
        assert mission["worker"] == "repo-code-bud"

        result = mission["result"]

        assert result["tests_passed"] is True
        assert result["commit_sha"]
        assert result["retries"] == 0
        assert result["attempts"] == 1
        assert result["working_tree_clean"] is True

        # Evidence covers the full pipeline.
        evidence_types = {
            item.get("type")
            for item in mission["evidence"]
            if isinstance(item, dict)
        }

        assert "llm_plan" in evidence_types
        assert "learning_retrieval" in evidence_types
        assert "edit" in evidence_types
        assert "worktree_cleanup" in evidence_types

        test_events = [
            item
            for item in mission["evidence"]
            if isinstance(item, dict)
            and item.get("cmd")
            and "pytest" in item["cmd"]
        ]

        assert test_events, "pytest runs must be in evidence"
        assert all(
            event["returncode"] == 0 for event in test_events
        )

        # Exactly one accepted commit on the mission branch; source
        # branch itself stays untouched.
        branch = result["branch"]

        assert git(repo, "rev-list", "--count", branch) == "2"

        final_code = git(repo, "show", f"{branch}:greet.py")

        assert "f'Hello {name}'" in final_code
        assert "'Hello ' + name" not in final_code

        assert git(repo, "rev-parse", "HEAD") == baseline

        # Worktree was removed by the Stage 7.5 success policy.
        assert not Path(result["worktree"]).exists()

        cleanup_events = [
            item
            for item in mission["evidence"]
            if isinstance(item, dict)
            and item.get("type") == "worktree_cleanup"
        ]

        assert cleanup_events
        assert cleanup_events[0]["action"] == "removed"

        # GET /missions/{id} returns the same final state.
        fetched = client.get(f"/api/v1/missions/{mission['id']}")

        assert fetched.status_code == 200
        assert fetched.json()["status"] == "PASS"
        assert (
            fetched.json()["result"]["commit_sha"]
            == result["commit_sha"]
        )

        # GET /missions/{id}/evidence returns the same evidence.
        evidence_response = client.get(
            f"/api/v1/missions/{mission['id']}/evidence"
        )

        assert evidence_response.status_code == 200
        assert evidence_response.json()["mission_id"] == mission["id"]
        assert evidence_response.json()["evidence"] == mission["evidence"]

        # Learning Core received a record tied to this mission.
        learning = client.get("/api/v1/learning").json()

        mission_records = [
            record
            for record in learning
            if record.get("mission_id") == mission["id"]
        ]

        assert mission_records, (
            "a learning record must exist for the mission"
        )
        assert mission_records[0]["outcome"] == "PASS"

        # Retrieval is available for the next mission's context.
        assert isinstance(captured.get("lessons"), str)

    finally:
        td.cleanup()
