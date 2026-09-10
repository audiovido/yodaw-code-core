"""Black-box failure classification acceptance.

Unknown capability -> BLOCKED. Oversized payload -> 413.
Cancel of unknown id -> 404. Cancel of finished mission -> 409.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission

client = TestClient(app)


def test_unknown_capability_blocks():
    response = client.post(
        "/api/v1/missions",
        json={"goal": "Impossible task", "capability": "unknown"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"


def test_blocked_mission_persists_with_error():
    created = client.post(
        "/api/v1/missions",
        json={"goal": "Blocked persists", "capability": "unknown"},
    ).json()
    fetched = client.get(f"/api/v1/missions/{created['id']}")
    assert fetched.status_code == 200
    body = fetched.json()
    assert body["status"] == "BLOCKED"
    assert "No worker for capability" in str(body.get("result", {}))


def test_oversized_goal_rejected_with_413():
    response = client.post(
        "/api/v1/missions",
        json={"goal": "x" * 5000, "capability": "code"},
    )
    assert response.status_code == 413


def test_cancel_unknown_mission_404():
    response = client.post("/api/v1/missions/m_does_not_exist/cancel")
    assert response.status_code == 404


def test_cancel_finished_mission_409():
    queued = client.post(
        "/api/v1/missions",
        json={"goal": "Cancel-after-finish is a conflict", "capability": "code"},
    ).json()
    mission = poll_mission(client, queued["id"], timeout=60.0)
    assert mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED")
    response = client.post(f"/api/v1/missions/{queued['id']}/cancel")
    assert response.status_code == 409
