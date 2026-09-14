import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission, make_smoke_repo, smoke_mission_payload

client = TestClient(app)

def test_health():
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "READY"

def test_code_mission(tmp_path):
    repo = make_smoke_repo(Path(tmp_path))
    payload = smoke_mission_payload("Prove YODAW Code Core execution works")
    payload["repo_path"] = repo

    response = client.post("/api/v1/missions", json=payload)

    assert response.status_code == 200

    queued = response.json()

    # Stage 8: POST returns QUEUED immediately.
    assert queued["status"] == "QUEUED"

    mission = poll_mission(client, queued["id"])

    assert mission["status"] == "PASS"
    assert mission["worker"] == "code-bud"
    assert len(mission["evidence"]) >= 1

def test_unknown_capability_blocks():
    response = client.post(
        "/api/v1/missions",
        json={
            "goal": "Impossible task",
            "capability": "unknown",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"