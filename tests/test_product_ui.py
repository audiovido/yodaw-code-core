"""Product UI (Worker C) contract tests.

Verifies the static mission console at GET /product exposes every
required control, and that the product mission view it consumes
carries observable mission state (goal, status, result, evidence,
links).
"""

import re

from fastapi.testclient import TestClient

from app.main import app


def make_client():
    return TestClient(app)


REQUIRED_IDS = [
    "goal",          # chat / mission input
    "repo",          # optional repo path
    "cap",           # capability selector
    "submit",        # submit mission
    "statusBadge",   # mission status
    "progressBar",   # progress / state
    "events",        # logs / events
    "evidence",      # evidence panel
    "cancelBtn",     # cancel
    "retryBtn",      # retry
    "copyResultBtn",  # copy buttons
    "errorBanner",   # clear error states
    "missionError",
    "resultJson",    # result panel
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