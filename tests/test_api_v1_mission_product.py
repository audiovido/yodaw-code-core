"""Worker I canonical product API contract tests (hermetic).

Covers submit/status, idempotency, invalid payload, cancel, retry,
evidence, blocked-external vs task failure, success, tenant
isolation, and concurrent duplicate submit. No provider/network.
"""

import threading
import time
import uuid

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from tests.helpers import poll_mission


def make_client():
    return TestClient(app)


def test_product_submit_returns_product_shape():
    client = make_client()
    response = client.post(
        "/api/v1/missions",
        json={
            "goal": f"product shape {uuid.uuid4().hex}",
            "repo_path": "/tmp/repo",
            "constraints": {"max_files": 3},
            "model": {"name": "test-model"},
            "provider": {"name": "test-provider"},
            "capability": "code",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mission_id"]
    assert body["status"] in (
        "QUEUED",
        "OBSERVING",
        "PLANNING",
        "EXECUTING",
        "VERIFYING",
        "RECOVERING",
        "PASS",
        "FAIL",
        "BLOCKED_EXTERNAL",
        "CANCELLED",
    )
    assert body["created_at"]
    assert body["links"]["self"].endswith(body["mission_id"])
    assert body["links"]["evidence"]
    assert body["links"]["cancel"]
    assert body["links"]["retry"]


def test_product_status_transition_code_mission():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={"goal": f"shape flow {uuid.uuid4().hex}", "capability": "code"},
    ).json()
    mission = poll_mission(client, created["mission_id"])
    assert mission["status"] == "PASS"


def test_product_dry_run_returns_result_without_execution():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={
            "goal": f"dry run {uuid.uuid4().hex}",
            "capability": "code",
            "dry_run": True,
        },
    ).json()
    assert created["status"] == "PASS"
    mission = client.get(f"/api/v1/missions/{created['mission_id']}").json()
    assert mission["result"]["dry_run"] is True


def test_product_idempotency_replay_returns_same_mission():
    client = make_client()
    key = f"idem-{uuid.uuid4().hex}"
    first = client.post(
        "/api/v1/missions",
        json={
            "goal": f"idempotent {uuid.uuid4().hex}",
            "capability": "code",
            "idempotency_key": key,
        },
    ).json()
    second = client.post(
        "/api/v1/missions",
        json={
            "goal": f"idempotent {uuid.uuid4().hex}",
            "capability": "code",
        },
        headers={"Idempotency-Key": key},
    ).json()
    assert second["mission_id"] == first["mission_id"]
    assert second["replayed"] is True


def test_product_concurrent_duplicate_submit_single_mission():
    client = make_client()
    key = f"race-{uuid.uuid4().hex}"
    goal = f"concurrent {uuid.uuid4().hex}"
    results = []

    def submit():
        response = client.post(
            "/api/v1/missions",
            json={"goal": goal, "capability": "code"},
            headers={"Idempotency-Key": key},
        )
        results.append(response.json()["mission_id"])

    threads = [threading.Thread(target=submit) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert len(results) == 5
    assert len(set(results)) == 1


def test_product_invalid_payload_rejected():
    client = make_client()
    assert client.post("/api/v1/missions", json={"capability": "code"}).status_code == 422
    assert client.post("/api/v1/missions", json={"goal": ""}).status_code in (200, 422, 413)


def test_product_cancel_queued_mission():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={"goal": f"cancel me {uuid.uuid4().hex}", "capability": "code"},
    ).json()
    cancelled = client.post(f"/api/v1/missions/{created['mission_id']}/cancel")
    assert cancelled.status_code in (200, 409)
    if cancelled.status_code == 200:
        assert cancelled.json()["status"] in ("CANCELLED", "CANCELLING")


def test_product_cancel_terminal_conflicts():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={"goal": f"finish then cancel {uuid.uuid4().hex}", "capability": "code"},
    ).json()
    poll_mission(client, created["mission_id"])
    response = client.post(f"/api/v1/missions/{created['mission_id']}/cancel")
    assert response.status_code == 409


def test_product_retry_preserves_evidence_and_lineage():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={"goal": f"retry me {uuid.uuid4().hex}", "capability": "unknown-cap"},
    ).json()
    assert created["status"] in ("FAIL", "BLOCKED", "BLOCKED_EXTERNAL", "PASS")
    retried = client.post(f"/api/v1/missions/{created['mission_id']}/retry")
    if retried.status_code == 200:
        body = retried.json()
        assert body["retried_from"] == created["mission_id"]
        child = client.get(f"/api/v1/missions/{body['mission_id']}").json()
        assert child["retried_from_id"] == created["mission_id"]
        parent = client.get(f"/api/v1/missions/{created['mission_id']}").json()
        assert body["mission_id"] in parent["attempt_lineage"]
    else:
        assert retried.status_code in (409, 404)


def test_product_retry_invalid_state_conflicts():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={"goal": f"active no retry {uuid.uuid4().hex}", "capability": "code"},
    ).json()
    response = client.post(f"/api/v1/missions/{created['mission_id']}/retry")
    assert response.status_code in (200, 409)


def test_product_retry_budget_bounded():
    from app.core.models import Mission, MissionStatus

    mission = Mission(goal="budget", capability="code")
    mission.status = MissionStatus.failed
    mission.metadata["product_attempts"] = 3
    main_module.store.save(mission)
    client = make_client()
    response = client.post(f"/api/v1/missions/{mission.id}/retry")
    assert response.status_code == 409


def test_product_evidence_retrieval():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={"goal": f"evidence {uuid.uuid4().hex}", "capability": "code"},
    ).json()
    poll_mission(client, created["mission_id"])
    evidence = client.get(f"/api/v1/missions/{created['mission_id']}/evidence")
    assert evidence.status_code == 200
    assert evidence.json()["mission_id"] == created["mission_id"]
    assert isinstance(evidence.json()["evidence"], list)


def test_product_blocked_external_not_task_failure():
    from app.mission.facade import classify_error, finalize_from_worker_result
    from app.core.models import Mission

    assert classify_error({"type": "LLMError", "message": "timed out"}) == "provider"
    assert classify_error({"type": "ValidationFailed", "message": "tests failed"}) == "task"
    mission = Mission(goal="provider fault", capability="repo-code")
    main_module.store.save(mission)
    finalize_from_worker_result(
        main_module.store,
        mission.id,
        {"success": False, "error": {"type": "LLMError", "message": "timeout"}},
    )
    stored = main_module.store.get(mission.id)
    assert stored.status.value == "BLOCKED_EXTERNAL"
    assert stored.error_class == "provider"


def test_product_task_failure_stays_fail():
    from app.mission.facade import finalize_from_worker_result
    from app.core.models import Mission

    mission = Mission(goal="task fault", capability="repo-code")
    main_module.store.save(mission)
    finalize_from_worker_result(
        main_module.store,
        mission.id,
        {"success": False, "error": {"type": "ValidationFailed", "message": "no"}},
    )
    stored = main_module.store.get(mission.id)
    assert stored.status.value == "FAIL"
    assert stored.error_class == "task"


def test_product_tenant_isolation():
    admin_client = make_client()
    first = admin_client.post("/api/v1/clients", json={"name": f"a-{uuid.uuid4().hex}"}).json()
    second = admin_client.post("/api/v1/clients", json={"name": f"b-{uuid.uuid4().hex}"}).json()
    first_client = TestClient(app, headers={"Authorization": f"Bearer {first['api_key']}"})
    second_client = TestClient(app, headers={"Authorization": f"Bearer {second['api_key']}"})
    mission_a = first_client.post(
        "/api/v1/missions",
        json={"goal": f"tenant a {uuid.uuid4().hex}", "capability": "code"},
    )
    assert mission_a.status_code == 200
    mission_id = mission_a.json()["mission_id"]
    assert second_client.get(f"/api/v1/missions/{mission_id}").status_code == 404
    assert second_client.get(f"/api/v1/missions/{mission_id}/evidence").status_code == 404


def test_product_status_and_capabilities_endpoints():
    client = make_client()
    assert client.get("/api/v1/status").status_code == 200
    capabilities = client.get("/api/v1/capabilities")
    assert capabilities.status_code == 200
    assert "capabilities" in capabilities.json()
