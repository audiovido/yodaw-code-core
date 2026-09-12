"""
Shared Stage 8 test helpers.

The Stage 8 API contract is asynchronous: POST returns QUEUED
immediately and execution completes independently. Tests that
verify end outcomes poll the mission with a deadline instead of
asserting on the POST response body.
"""

import time
from typing import Optional

from fastapi.testclient import TestClient

TERMINAL = {"PASS", "FAIL", "BLOCKED", "CANCELLED"}


def poll_mission(
    client: TestClient,
    mission_id: str,
    timeout: float = 30.0,
    interval: float = 0.1,
    headers: Optional[dict] = None,
):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/missions/{mission_id}", headers=headers
        )
        assert response.status_code == 200

        mission = response.json()

        if mission["status"] in TERMINAL:
            return mission

        time.sleep(interval)

    raise AssertionError(
        f"mission {mission_id} did not reach a terminal state "
        f"within {timeout}s"
    )
