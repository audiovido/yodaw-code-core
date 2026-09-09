"""
Stage 8.7/8.8/8.10: authentication, secret hygiene, mission
events, and the async API contract.
"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from tests.helpers import poll_mission


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def auth_client(monkeypatch):
    key = f"test-key-{uuid.uuid4().hex}"
    monkeypatch.setenv("YODAW_API_KEY", key)
    yield key, TestClient(app)
    monkeypatch.delenv("YODAW_API_KEY", raising=False)


def test_health_is_open_without_key(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["auth"] == "local-dev"


def test_missing_key_rejected(auth_client):
    _, client = auth_client

    response = client.post(
        "/api/v1/missions",
        json={"goal": "g", "capability": "code"},
    )

    assert response.status_code == 401


def test_invalid_key_rejected(auth_client):
    _, client = auth_client

    response = client.post(
        "/api/v1/missions",
        json={"goal": "g", "capability": "code"},
        headers={"Authorization": "Bearer wrong-key"},
    )

    assert response.status_code == 401


def test_valid_key_accepted_and_health_open(auth_client):
    key, client = auth_client

    health = client.get("/api/v1/health")

    assert health.status_code == 200
    assert health.json()["auth"] == "key"

    headers = {"Authorization": f"Bearer {key}"}

    response = client.post(
        "/api/v1/missions",
        json={"goal": f"authed {uuid.uuid4().hex}", "capability": "code"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"

    mission = poll_mission(client, response.json()["id"], headers=headers)

    assert mission["status"] == "PASS"


def test_key_never_appears_in_responses_or_evidence(auth_client):
    key, client = auth_client
    headers = {"Authorization": f"Bearer {key}"}

    created = client.post(
        "/api/v1/missions",
        json={"goal": f"secret check {uuid.uuid4().hex}", "capability": "code"},
        headers=headers,
    )

    mission_id = created.json()["id"]
    mission = poll_mission(client, mission_id, headers=headers)

    # The key must not leak into any API payload.
    assert key not in created.text
    assert key not in str(mission)

    evidence = client.get(
        f"/api/v1/missions/{mission_id}/evidence", headers=headers
    )
    events = client.get(
        f"/api/v1/missions/{mission_id}/events", headers=headers
    )

    assert key not in evidence.text
    assert key not in events.text


def test_async_post_returns_before_execution_completes(client, monkeypatch):
    """
    Core Stage 8 guarantee: POST latency does not wait for
    execution, even when the worker blocks for a long time.
    """
    import threading

    from app.core.models import Mission
    from app.storage.sqlite_store import MissionStore
    from app.runtime.repo_leases import RepoLeaseManager
    from app.runtime.coordinator import Coordinator

    release = threading.Event()
    started = threading.Event()

    class BlockedWorker:
        name = "blocked-bud"
        capabilities = {"code"}

        def health(self):
            return {"name": self.name}

        def execute(self, goal):
            started.set()
            release.wait(timeout=10)
            return {"success": True, "output": {}, "evidence": []}

    coordinator = Coordinator(
        store=main_module.store,
        leases=RepoLeaseManager(main_module.store.path),
        registry=type("R", (), {"find": lambda self, c: BlockedWorker(), "status": lambda self: []})(),
        id_prefix="blocked",
    )

    monkeypatch.setattr(
        main_module, "get_coordinator", lambda: coordinator
    )

    coordinator.start()

    try:
        t0 = time.monotonic()

        response = client.post(
            "/api/v1/missions",
            json={"goal": "blocked worker", "capability": "code"},
        )

        elapsed = time.monotonic() - t0

        assert response.status_code == 200
        assert response.json()["status"] == "QUEUED"

        # POST must return long before the worker even starts.
        assert elapsed < 1.0, elapsed

        assert started.wait(timeout=5), "mission should start soon after"

        events = client.get(
            f"/api/v1/missions/{response.json()['id']}/events"
        )

        assert events.status_code == 200

        event_types = [
            e["event_type"] for e in events.json()["events"]
        ]

        assert "mission.queued" in event_types
        assert "mission.started" in event_types

    finally:
        release.set()
        coordinator.stop()


def test_cancel_endpoint_running_mission_flow(client, monkeypatch):
    """
    Cancel while a mission is mid-flight: the API answers
    CANCELLING immediately, the event is recorded, and after the
    worker reaches its checkpoint the mission ends CANCELLED.
    """
    import threading

    from app.storage.sqlite_store import MissionStore
    from app.runtime.repo_leases import RepoLeaseManager
    from app.runtime.coordinator import Coordinator

    release = threading.Event()

    class BlockedWorker:
        name = "blocked-bud"
        capabilities = {"code"}

        def health(self):
            return {"name": self.name}

        def execute(self, goal, metadata=None):
            release.wait(timeout=10)

            # Simulate a cooperative worker honoring the cancel
            # flag at its checkpoint, exactly like the real one.
            store = (metadata or {}).get("_event_store")
            mission_id = (metadata or {}).get("mission_id")

            if store is not None and mission_id:
                mission = store.get(mission_id)

                if mission is not None and mission.cancel_requested:
                    return {
                        "success": False,
                        "output": {},
                        "evidence": [],
                        "error": {
                            "type": "Cancelled",
                            "message": "observed at checkpoint",
                        },
                        "retryable": False,
                    }

            return {"success": True, "output": {}, "evidence": []}

    coordinator = Coordinator(
        store=main_module.store,
        leases=RepoLeaseManager(main_module.store.path),
        registry=type("R", (), {"find": lambda self, c: BlockedWorker(), "status": lambda self: []})(),
        id_prefix="cancelapi",
    )

    monkeypatch.setattr(
        main_module, "get_coordinator", lambda: coordinator
    )

    coordinator.start()

    try:
        created = client.post(
            "/api/v1/missions",
            json={"goal": "cancel me mid-flight", "capability": "code"},
        )

        mission_id = created.json()["id"]

        # Wait until the mission is RUNNING, then cancel.
        deadline = time.monotonic() + 5

        while time.monotonic() < deadline:
            mission = client.get(
                f"/api/v1/missions/{mission_id}"
            ).json()

            if mission["status"] == "RUNNING":
                break

            time.sleep(0.05)

        cancelled = client.post(
            f"/api/v1/missions/{mission_id}/cancel"
        )

        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLING"

    finally:
        release.set()
        coordinator.stop()

    mission = client.get(f"/api/v1/missions/{mission_id}").json()

    assert mission["status"] == "CANCELLED"

    events = client.get(
        f"/api/v1/missions/{mission_id}/events"
    ).json()

    event_types = [e["event_type"] for e in events["events"]]

    assert "mission.queued" in event_types
    assert "mission.cancel_requested" in event_types
    assert "mission.cancelled" in event_types


def test_cancel_unknown_mission_404(client):
    response = client.post("/api/v1/missions/m_doesnotexist/cancel")

    assert response.status_code == 404


def test_events_endpoint_404_for_unknown_mission(client):
    response = client.get("/api/v1/missions/m_doesnotexist/events")

    assert response.status_code == 404


def test_runtime_status_endpoint(client):
    response = client.get("/api/v1/runtime/status")

    assert response.status_code == 200
    assert "missions" in response.json()
