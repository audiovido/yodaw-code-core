"""Black-box router acceptance.

Covers only what this base demonstrably supports:
capability dispatch (code -> code-bud) and unknown -> BLOCKED.
Worker N routing beyond this is marked PENDING_WORKER_N.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission

client = TestClient(app)


def test_router_dispatches_code_to_code_bud():
    queued = client.post(
        "/api/v1/missions",
        json={"goal": "Router dispatches code", "capability": "code"},
    ).json()
    assert queued["status"] == "QUEUED"
    mission = poll_mission(client, queued["id"], timeout=60.0)
    assert mission["worker"] == "code-bud"
    assert mission["status"] == "PASS"


def test_router_blocks_unroutable_capability():
    response = client.post(
        "/api/v1/missions",
        json={"goal": "No route exists", "capability": "no-such-capability"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "BLOCKED"


def test_router_registry_lists_supported_capabilities():
    response = client.get("/api/v1/workers")
    assert response.status_code == 200
    caps = {c for w in response.json() for c in w["capabilities"]}
    assert "code" in caps
