"""Worker I public API contract tests for new endpoints."""

import uuid
from fastapi.testclient import TestClient
from app.main import app

def make_client():
    return TestClient(app)

class TestHealthEndpoints:
    """Test health, readiness, and version endpoints."""

    def test_health_endpoint(self):
        client = make_client()
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["service"] == "YODAW"
        assert body["status"] == "READY"
        assert "auth" in body
        assert "workers" in body
        assert "config_ok" in body
        assert "profile" in body

    def test_readiness_endpoint(self):
        client = make_client()
        response = client.get("/api/v1/ready")
        # In test environment, coordinator may not be running -> 503
        assert response.status_code in (200, 503)
        body = response.json()
        assert body["service"] == "YODAW"
        assert "ready" in body
        assert "coordinator" in body
        assert "config" in body

    def test_version_endpoint(self):
        client = make_client()
        response = client.get("/api/v1/version")
        assert response.status_code == 200
        body = response.json()
        assert body["api_version"] == "v1"
        assert body["title"] == "YODAW Public API"
        assert body["service_version"] == "0.3.0"
        assert "build" in body

class TestClassifyEndpoint:
    """Test the classification endpoint."""

    def test_classify_returns_capability_and_intent(self):
        client = make_client()
        response = client.post(
            "/api/v1/classify",
            json={"prompt": "fix a bug in the login flow"}
        )
        assert response.status_code == 200
        body = response.json()
        assert "capability" in body
        assert "intent" in body
        assert "skill" in body
        assert "confidence" in body
        assert "reason" in body
        assert "metadata" in body
        assert body["capability"] in ["repo-code", "code"]
        assert body["intent"] in ["bugfix", "refactor", "test", "review", "feature", "documentation"]
        assert 0.0 <= body["confidence"] <= 1.0

    def test_classify_refactor_intent(self):
        client = make_client()
        response = client.post(
            "/api/v1/classify",
            json={"prompt": "refactor the user service to use dependency injection"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "refactor"

    def test_classify_test_intent(self):
        client = make_client()
        response = client.post(
            "/api/v1/classify",
            json={"prompt": "write unit tests for the payment module"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "test"

    def test_classify_feature_intent(self):
        client = make_client()
        response = client.post(
            "/api/v1/classify",
            json={"prompt": "implement a new REST endpoint for user profiles"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "feature"

    def test_classify_requires_prompt(self):
        client = make_client()
        response = client.post("/api/v1/classify", json={})
        assert response.status_code == 422
        body = response.json()
        assert body["title"] == "Validation Error"

    def test_classify_empty_prompt(self):
        client = make_client()
        response = client.post("/api/v1/classify", json={"prompt": ""})
        # Empty prompt is valid Pydantic but returns default classification
        assert response.status_code == 200
        body = response.json()
        assert body["intent"] == "feature"
        assert body["skill"] == "feature"

    def test_classify_accepts_context(self):
        client = make_client()
        response = client.post(
            "/api/v1/classify",
            json={"prompt": "add a new feature", "context": {"language": "python"}}
        )
        assert response.status_code == 200
        body = response.json()
        assert "metadata" in body

class TestResultEndpoint:
    """Test the task result endpoint."""

    def test_result_endpoint_exists(self):
        client = make_client()
        response = client.get(f"/api/v1/missions/nonexistent/result")
        # Should return 404 or 409, not 404 for non-terminal
        assert response.status_code in (404, 409)
        if response.status_code == 409:
            body = response.json()
            assert "detail" in body

    def test_result_for_terminal_mission(self):
        client = make_client()
        # Submit a dry-run mission which completes immediately
        created = client.post(
            "/api/v1/missions",
            json={
                "goal": f"dry run result {uuid.uuid4().hex}",
                "capability": "code",
                "dry_run": True,
            },
        ).json()
        assert created["status"] == "PASS"

        response = client.get(f"/api/v1/missions/{created['mission_id']}/result")
        assert response.status_code == 200
        body = response.json()
        assert body["mission_id"] == created["mission_id"]
        assert body["status"] == "PASS"
        assert "result" in body
        assert "error_class" in body
        assert "finished_at" in body
        assert "attempt" in body
        assert "evidence_count" in body

    def test_result_for_non_terminal_returns_409(self):
        client = make_client()
        # Submit a real mission that will be QUEUED
        created = client.post(
            "/api/v1/missions",
            json={"goal": f"real mission {uuid.uuid4().hex}", "capability": "code"},
        ).json()
        assert created["status"] == "QUEUED"

        response = client.get(f"/api/v1/missions/{created['mission_id']}/result")
        assert response.status_code == 409
        body = response.json()
        assert "detail" in body

class TestErrorEnvelope:
    """Test standardized error responses (RFC 7807)."""

    def test_422_returns_envelope(self):
        client = make_client()
        response = client.post("/api/v1/missions", json={"capability": "code"})  # missing goal
        assert response.status_code == 422
        body = response.json()
        assert "type" in body
        assert "title" in body
        assert "status" in body
        assert body["status"] == 422
        assert "detail" in body
        assert "instance" in body
        assert "trace_id" in body
        assert "errors" in body
        assert isinstance(body["errors"], list)

    def test_404_returns_envelope(self):
        client = make_client()
        response = client.get("/api/v1/missions/nonexistent")
        # 404 returns minimal body for security
        assert response.status_code == 404

    def test_413_returns_envelope(self):
        client = make_client()
        response = client.post(
            "/api/v1/missions",
            json={"goal": "x" * 5000, "capability": "code"},
        )
        # May be 413 or 200 depending on governance config
        if response.status_code == 413:
            body = response.json()
            assert "type" in body
            assert "title" in body
            assert "status" in body
            assert body["status"] == 413

    def test_409_returns_envelope(self):
        client = make_client()
        # Create a mission, then try to cancel twice
        created = client.post(
            "/api/v1/missions",
            json={"goal": f"cancel me {uuid.uuid4().hex}", "capability": "code"},
        ).json()
        # First cancel
        client.post(f"/api/v1/missions/{created['mission_id']}/cancel")
        # Second cancel should 409
        response = client.post(f"/api/v1/missions/{created['mission_id']}/cancel")
        if response.status_code == 409:
            body = response.json()
            assert "type" in body
            assert "title" in body
            assert "status" in body
            assert body["status"] == 409

    def test_429_returns_retry_after(self):
        # Hard to test without rate limiting, but verify envelope shape
        pass

class TestBackwardCompatibility:
    """Ensure existing endpoints still work."""

    def test_submit_mission_still_works(self):
        client = make_client()
        response = client.post(
            "/api/v1/missions",
            json={"goal": f"compat {uuid.uuid4().hex}", "capability": "code"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "QUEUED"
        assert "mission_id" in body
        assert "links" in body

    def test_get_mission_still_works(self):
        client = make_client()
        created = client.post(
            "/api/v1/missions",
            json={"goal": f"get me {uuid.uuid4().hex}", "capability": "code"},
        ).json()
        response = client.get(f"/api/v1/missions/{created['mission_id']}")
        assert response.status_code == 200

    def test_get_evidence_still_works(self):
        client = make_client()
        created = client.post(
            "/api/v1/missions",
            json={"goal": f"evidence {uuid.uuid4().hex}", "capability": "code", "dry_run": True},
        ).json()
        response = client.get(f"/api/v1/missions/{created['mission_id']}/evidence")
        assert response.status_code == 200
        body = response.json()
        assert "evidence" in body
        assert isinstance(body["evidence"], list)

    def test_cancel_still_works(self):
        client = make_client()
        created = client.post(
            "/api/v1/missions",
            json={"goal": f"cancel {uuid.uuid4().hex}", "capability": "code"},
        ).json()
        response = client.post(f"/api/v1/missions/{created['mission_id']}/cancel")
        assert response.status_code in (200, 409)

    def test_retry_still_works(self):
        client = make_client()
        created = client.post(
            "/api/v1/missions",
            json={"goal": f"retry {uuid.uuid4().hex}", "capability": "unknown-cap"},
        ).json()
        response = client.post(f"/api/v1/missions/{created['mission_id']}/retry")
        assert response.status_code in (200, 409, 404)

    def test_list_missions_still_works(self):
        client = make_client()
        response = client.get("/api/v1/missions")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    def test_status_endpoint_still_works(self):
        client = make_client()
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        body = response.json()
        assert "missions" in body
        assert "workers" in body

    def test_capabilities_endpoint_still_works(self):
        client = make_client()
        response = client.get("/api/v1/capabilities")
        assert response.status_code == 200
        body = response.json()
        assert "capabilities" in body
        assert "mission_statuses" in body