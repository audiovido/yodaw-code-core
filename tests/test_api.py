import time

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission


client = TestClient(app)


def test_health():
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "READY"


def test_code_mission():
    response = client.post(
        "/api/v1/missions",
        json={
            "goal": "Prove YODAW Code Core execution works",
            "capability": "code",
        },
    )

    assert response.status_code == 200

    queued = response.json()

    # Stage 8: POST returns QUEUED immediately.
    assert queued["status"] == "QUEUED"

    payload = poll_mission(client, queued["id"])

    assert payload["status"] == "PASS"
    assert payload["worker"] == "code-bud"
    assert len(payload["evidence"]) >= 1


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
