"""Black-box computer acceptance.

Covers only what this base demonstrably supports:
worker filesystem evidence (workspace, git diff, pytest runs).
Native computer-control beyond this is marked PENDING_WORKER_N.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission

client = TestClient(app)

def _evidence_text(evidence) -> str:
    return " ".join(str(item) for item in evidence)

def test_code_worker_leaves_filesystem_evidence():
    queued = client.post(
        "/api/v1/missions",
        json={"goal": "Computer evidence check", "capability": "code"},
    ).json()
    mission = poll_mission(client, queued["id"], timeout=60.0)
    assert mission["status"] == "PASS"
    blob = _evidence_text(mission["evidence"])
    # When there is no repo_path, CodeWorker returns a planning result.
    # We expect to see evidence of planning, not of running tests or git.
    assert "CodeWorker planning started" in blob
    assert "CodeWorker planning finished" in blob

def test_code_worker_reports_workspace_and_commit():
    queued = client.post(
        "/api/v1/missions",
        json={"goal": "Computer workspace check", "capability": "code"},
    ).json()
    mission = poll_mission(client, queued["id"], timeout=60.0)
    assert mission["status"] == "PASS"
    result = mission["result"]
    # When there is no repo_path, CodeWorker returns a planning result.
    # We expect to see evidence of planning, not of running tests or git.
    assert "repo" in result
    assert result["repo"]["observed"] is False
    assert "plan" in result
    assert result["plan"]["planned"] is True
    blob = _evidence_text(mission["evidence"])
    assert "CodeWorker planning started" in blob
    assert "CodeWorker planning finished" in blob