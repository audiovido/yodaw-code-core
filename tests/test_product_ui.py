"""Product UI (Worker C) contract and integration tests.

Verifies the static mission console at GET /product exposes every
required control, click and Enter key event bindings, accessibility
and responsive structure, and that the product mission view it consumes
carries observable mission state (goal, status, result, evidence, links).
Covers submit, Enter submit, duplicate idempotency submit, invalid repo,
backend offline, cancel, retry, responsive styling, accessibility, and
real acceptance execution flow.
"""

import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app
from tests.helpers import poll_mission


def make_client():
    return TestClient(app)


REQUIRED_IDS = [
    "goal",           # chat / mission input
    "repo",           # optional repo path
    "cap",            # capability selector
    "submit",         # submit mission
    "statusBadge",    # mission status
    "progressBar",    # progress / state
    "events",         # logs / events
    "evidence",       # evidence panel
    "cancelBtn",      # cancel
    "retryBtn",       # retry
    "copyResultBtn",  # copy buttons
    "errorBanner",    # clear error states
    "missionError",
    "resultJson",     # result panel
]


def test_product_page_serves_all_required_controls():
    client = make_client()
    response = client.get("/product")
    assert response.status_code == 200
    html = response.text
    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in html, f"missing required control #{element_id}"
    # Terminal statuses the console must render distinctly.
    for status in ("PASS", "FAIL", "BLOCKED", "BLOCKED_EXTERNAL", "CANCELLED"):
        assert f"badge.{status}" in html, f"missing status style for {status}"


def test_product_view_carries_goal_and_terminal_state():
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={
            "goal": "ui contract goal",
            "capability": "code",
            "dry_run": True,
        },
    ).json()
    mission = client.get(f"/api/v1/missions/{created['mission_id']}").json()
    assert mission["goal"] == "ui contract goal"
    assert mission["status"] == "PASS"
    assert mission["result"]["dry_run"] is True
    assert mission["links"]["retry"]
    assert mission["links"]["evidence"]
    assert re.match(r"m_[0-9a-f]{12}", mission["mission_id"])


def test_product_page_lists_missions_with_goal():
    client = make_client()
    # Ensure at least one mission exists.
    client.post(
        "/api/v1/missions",
        json={"goal": "listable goal", "capability": "code", "dry_run": True},
    )
    missions = client.get("/api/v1/missions?limit=10&offset=0").json()
    assert isinstance(missions, list)
    assert all("goal" in m and "status" in m for m in missions)


def test_submit_button_and_enter_key_event_listeners_bound():
    """Verify that both the submit button click listener and Enter key listener are wired in static UI."""
    client = make_client()
    response = client.get("/product")
    assert response.status_code == 200
    html = response.text

    # Submit button click listener binding check
    assert 'submitBtn.addEventListener("click", submitMission)' in html or '$("submit").addEventListener("click", submitMission)' in html
    # Enter keydown submit listener binding check
    assert 'e.key === "Enter"' in html
    assert "submitMission()" in html
    # Dropdown change listener
    assert 'capEl.addEventListener("change", updateSubmit)' in html


def test_duplicate_submission_and_idempotency_replay():
    """Duplicate submit with same content/idempotency key returns replayed mission cleanly."""
    client = make_client()
    key = f"ui-test-key-{uuid.uuid4().hex}"
    goal = f"Idempotent UI task {uuid.uuid4().hex}"
    first = client.post(
        "/api/v1/missions",
        json={"goal": goal, "capability": "code", "dry_run": True, "idempotency_key": key},
    ).json()
    assert first["replayed"] is False

    second = client.post(
        "/api/v1/missions",
        json={"goal": goal, "capability": "code", "dry_run": True, "idempotency_key": key},
    ).json()
    assert second["replayed"] is True
    assert second["mission_id"] == first["mission_id"]


def test_invalid_repository_rejection_and_error_handling(monkeypatch):
    """Submitting with an unauthorized repo path outside allowed roots returns 403 RepoNotAllowed."""
    client = make_client()
    monkeypatch.setenv("YODAW_REPO_ROOTS", "/var/allowed_workspace_root")
    response = client.post(
        "/api/v1/missions",
        json={
            "goal": "Invalid repo path test",
            "capability": "code",
            "repo_path": "/etc/forbidden_unauthorized_dir",
        },
    )
    assert response.status_code == 403
    assert "outside the authorized roots" in response.json().get("detail", "")


def test_invalid_non_git_repository_fails_gracefully():
    """Submitting a repo-code mission against a non-git directory fails gracefully with RepoError."""
    client = make_client()
    with tempfile.TemporaryDirectory() as td:
        notgit = Path(td) / "not_a_git_repo"
        notgit.mkdir()
        created = client.post(
            "/api/v1/missions",
            json={
                "goal": "Modify file.txt to say hello",
                "capability": "repo-code",
                "repo_path": str(notgit),
            },
        ).json()
        mission = poll_mission(client, created["mission_id"], timeout=30.0)
        assert mission["status"] == "FAIL"
        err = (mission.get("result") or {}).get("error") or {}
        assert "not a git repository" in err.get("message", "").lower()


def test_backend_offline_status_degradation():
    """Verify static UI script has offline handling when status check fails."""
    client = make_client()
    response = client.get("/product")
    assert response.status_code == 200
    html = response.text
    # Check that error catch sets offline text and bad class
    assert '"offline"' in html
    assert 'chip.classList.add("bad")' in html


def test_mission_cancellation_flow():
    """Create a mission and cancel it, verifying CANCELLED status."""
    client = make_client()
    created = client.post(
        "/api/v1/missions",
        json={"goal": f"cancel flow {uuid.uuid4().hex}", "capability": "code"},
    ).json()
    mid = created["mission_id"]
    res = client.post(f"/api/v1/missions/{mid}/cancel")
    assert res.status_code in (200, 409)
    if res.status_code == 200:
        assert res.json()["status"] in ("CANCELLED", "CANCELLING")


def test_retry_preserves_lineage_and_metadata():
    """Retry a failed/terminal mission and verify retried lineage and attempt increment."""
    from app.core.models import Mission, MissionStatus

    client = make_client()
    mission = Mission(goal="failing mission to retry", capability="code")
    mission.status = MissionStatus.failed
    mission.metadata["product_attempts"] = 1
    main_module.store.save(mission)

    retried_res = client.post(f"/api/v1/missions/{mission.id}/retry")
    assert retried_res.status_code == 200
    retried_body = retried_res.json()
    assert retried_body["retried_from"] == mission.id
    child = client.get(f"/api/v1/missions/{retried_body['mission_id']}").json()
    assert child["retried_from_id"] == mission.id
    assert child["attempts"] == 2


def test_responsive_layout_and_accessibility_attributes():
    """Verify accessibility ARIA attributes and responsive styles in HTML."""
    client = make_client()
    response = client.get("/product")
    assert response.status_code == 200
    html = response.text

    # ARIA roles and attributes
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert 'role="alert"' in html
    assert 'aria-live="assertive"' in html
    assert 'role="progressbar"' in html
    assert 'role="tablist"' in html
    assert 'role="tab"' in html
    assert 'aria-selected=' in html

    # Viewport & Responsive styles
    assert '<meta name="viewport"' in html
    assert '@media (max-width: 900px)' in html
    assert '@media (max-width: 600px)' in html


def test_real_acceptance_flow_submit_to_evidence():
    """Real acceptance flow: submit -> inspect -> repair/validate -> commit -> evidence."""
    client = make_client()
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        repo.mkdir()

        def git(*args):
            subprocess.run(
                ["git", *args], cwd=repo, check=True,
                capture_output=True, text=True,
            )

        git("init")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
        (repo / "test_calc.py").write_text(
            "from calc import add\n\n"
            "def test_add():\n"
            "    assert add(2, 3) == 5\n"
        )
        (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
        git("add", ".")
        git("commit", "-m", "baseline with failing test")

        # Submit deterministic repair mission
        goal = "Fix failing calculator test"
        resp = client.post(
            "/api/v1/missions",
            json={
                "goal": goal,
                "capability": "repo-code",
                "repo_path": str(repo),
                "metadata": {
                    "repo_path": str(repo),
                    "target_file": "calc.py",
                    "find": "return a - b",
                    "replace": "return a + b",
                },
            },
        )
        assert resp.status_code == 200
        mid = resp.json()["mission_id"]

        # Poll to terminal state
        mission = poll_mission(client, mid, timeout=60.0)
        assert mission["status"] == "PASS"
        assert mission["worker"] == "repo-code-bud"
        assert len(mission["evidence"]) > 0

        # Verify evidence endpoint matches
        ev_resp = client.get(f"/api/v1/missions/{mid}/evidence")
        assert ev_resp.status_code == 200
        evidence_list = ev_resp.json()["evidence"]
        assert len(evidence_list) >= 1

        # Check commit on branch
        branch = mission["result"]["branch"]
        log_out = subprocess.run(
            ["git", "log", "-n", "1", "--oneline", branch],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
        assert len(log_out.strip()) > 0
