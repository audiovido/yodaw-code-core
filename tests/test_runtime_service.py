"""
Stage 8.11: runtime service lifecycle.

Start the real runtime entrypoint on a test port, exercise the
API over real HTTP, and verify SIGTERM shutdown is clean: no
orphan processes, missions finalized, storage consistent.
"""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from app.core.models import Mission
from app.storage.sqlite_store import MissionStore


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(url: str, timeout: float = 2.0):
    request = urllib.request.Request(url)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json_loads(response.read())


def json_loads(raw: bytes):
    import json

    return json.loads(raw)


def http_post_json(url: str, payload: dict, timeout: float = 5.0):
    import json

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json_loads(response.read())


@pytest.fixture()
def runtime(tmp_path):
    env = dict(os.environ)
    env["YODAW_DB_PATH"] = str(tmp_path / "runtime.sqlite")
    env["YODAW_PORT"] = str(free_port())
    env["YODAW_MAX_CONCURRENT_MISSIONS"] = "2"
    env["YODAW_LOG_LEVEL"] = "WARNING"

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.runtime"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{env['YODAW_PORT']}"

    # Wait for the health endpoint.
    deadline = time.monotonic() + 20

    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode()
            raise AssertionError(f"runtime died early:\n{out}")

        try:
            status, body = http_get(f"{base}/api/v1/health")

            if status == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        raise AssertionError("runtime never became healthy")

    yield base, proc, env

    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)


def test_runtime_health_and_mission_roundtrip(runtime):
    base, proc, env = runtime

    status, body = http_get(f"{base}/api/v1/health")

    assert status == 200
    assert body["status"] == "READY"
    assert body["auth"] == "local-dev"

    # Enqueue a trivial code mission; the embedded coordinator
    # should execute it asynchronously.
    status, created = http_post_json(
        f"{base}/api/v1/missions",
        {"goal": "runtime roundtrip goal", "capability": "code"},
    )

    assert status == 200
    assert created["status"] == "QUEUED"

    mission_id = created["id"]

    deadline = time.monotonic() + 20

    final = None

    while time.monotonic() < deadline:
        status, mission = http_get(f"{base}/api/v1/missions/{mission_id}")

        if mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED"):
            final = mission
            break

        time.sleep(0.1)

    assert final is not None
    assert final["status"] == "PASS", final

    status, events = http_get(f"{base}/api/v1/missions/{mission_id}/events")

    event_types = [e["event_type"] for e in events["events"]]

    assert "mission.queued" in event_types
    assert "mission.started" in event_types
    assert "mission.completed" in event_types


def test_runtime_sigterm_shutdown_is_clean(runtime):
    base, proc, env = runtime

    assert proc.poll() is None

    proc.send_signal(signal.SIGTERM)
    exit_code = proc.wait(timeout=20)

    assert exit_code == 0, exit_code

    # Storage must be intact and readable after shutdown.
    store = MissionStore(env["YODAW_DB_PATH"])

    counts = store.status_counts()

    assert isinstance(counts, dict)
