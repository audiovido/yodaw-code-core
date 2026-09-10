"""Black-box mission API acceptance over HTTP (TestClient as transport).

Asserts the documented async contract only:
POST -> QUEUED, poll GET -> terminal state, evidence/events endpoints.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission

client = TestClient(app)


def test_health_ready():
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "READY"
    assert body["service"] == "YODAW"


def test_workers_advertise_code_capability():
    response = client.get("/api/v1/workers")
    assert response.status_code == 200
    workers = response.json()
    names = {w["name"] for w in workers}
    assert "code-bud" in names
    caps = {c for w in workers for c in w["capabilities"]}
    assert "code" in caps


def test_code_mission_passes_with_evidence():
    response = client.post(
        "/api/v1/missions",
        json={"goal": "Prove YODAW Code Core execution works", "capability": "code"},
    )
    assert response.status_code == 200
    queued = response.json()
    assert queued["status"] == "QUEUED"

    mission = poll_mission(client, queued["id"], timeout=60.0)
    assert mission["status"] == "PASS"
    assert mission["worker"] == "code-bud"
    assert len(mission["evidence"]) >= 1


def test_get_mission_returns_terminal_state():
    queued = client.post(
        "/api/v1/missions",
        json={"goal": "Acceptance GET round-trip", "capability": "code"},
    ).json()
    mission = poll_mission(client, queued["id"], timeout=60.0)
    fetched = client.get(f"/api/v1/missions/{mission['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == mission["status"]


def test_evidence_endpoint_matches_mission():
    queued = client.post(
        "/api/v1/missions",
        json={"goal": "Acceptance evidence round-trip", "capability": "code"},
    ).json()
    mission = poll_mission(client, queued["id"], timeout=60.0)
    response = client.get(f"/api/v1/missions/{mission['id']}/evidence")
    assert response.status_code == 200
    body = response.json()
    assert body["mission_id"] == mission["id"]
    assert body["evidence"] == mission["evidence"]


def test_events_endpoint_returns_list():
    queued = client.post(
        "/api/v1/missions",
        json={"goal": "Acceptance events round-trip", "capability": "code"},
    ).json()
    mission = poll_mission(client, queued["id"], timeout=60.0)
    response = client.get(f"/api/v1/missions/{mission['id']}/events")
    assert response.status_code == 200
    body = response.json()
    assert body["mission_id"] == mission["id"]
    assert isinstance(body["events"], list)


def test_unknown_mission_returns_404():
    response = client.get("/api/v1/missions/m_does_not_exist")
    assert response.status_code == 404
