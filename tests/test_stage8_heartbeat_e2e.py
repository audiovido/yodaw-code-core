"""
Stage 8.5 runtime E2E: heartbeats advance during a long mission.

Short hermetic end-to-end proof for the heartbeat fix, run against
the real runtime process (python -m app.runtime, real HTTP, real
coordinator, real worker, real provider path):

- a deliberately slow fake Ollama planner blocks the worker for
  several heartbeat intervals (no real model, no Ollama required)
- POST returns QUEUED immediately
- while RUNNING, the mission's heartbeat_at advances repeatedly
  on the persisted payload that the API serves
- the API stays responsive the whole time
- the mission ends PASS with exactly one commit

This is the runtime-level counterpart to the in-process heartbeat
tests in test_heartbeat_hardening.py.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from test_runtime_service import free_port, http_get, http_post_json


PLAN = {
    "action": "edit",
    "edits": [
        {
            "target_file": "greet.py",
            "find": "return 'Hello ' + name",
            "replace": "return f'Hello {name}'",
        }
    ],
    "reason": "use an f-string without changing behavior",
}

PLANNER_DELAY_SECONDS = 4.0


class SlowOllamaHandler(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.calls.append(time.time())

        # Deliberately slow "planner": blocks the worker thread for
        # several heartbeat intervals, like a CPU-only model would.
        time.sleep(PLANNER_DELAY_SECONDS)

        body = json.dumps(
            {"message": {"content": json.dumps(PLAN)}}
        ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def slow_fake_ollama():
    server = HTTPServer(("127.0.0.1", 0), SlowOllamaHandler)
    SlowOllamaHandler.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", SlowOllamaHandler.calls
    server.shutdown()


@pytest.fixture()
def runtime_with_slow_planner(tmp_path, slow_fake_ollama):
    fake_url, calls = slow_fake_ollama
    project_root = Path(__file__).resolve().parents[1]

    env = dict(os.environ)
    env.update(
        {
            "YODAW_DB_PATH": str(tmp_path / "hb_e2e.sqlite"),
            "YODAW_PORT": str(free_port()),
            "YODAW_LLM_STYLE": "ollama",
            "YODAW_LLM_BASE_URL": fake_url,
            "YODAW_LLM_MODEL": "fake-slow-planner",
            "YODAW_LLM_TIMEOUT_SECONDS": "60",
            "YODAW_PROVIDER_MAX_RETRIES": "1",
            "YODAW_PROVIDER_BACKOFF_SECONDS": "0.05",
            "YODAW_HEARTBEAT_SECONDS": "1",
            "YODAW_ENABLE_GITHUB": "false",
            "YODAW_LOG_LEVEL": "WARNING",
        }
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.runtime"],
        cwd=str(project_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )

    base = f"http://127.0.0.1:{env['YODAW_PORT']}"

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            status, _ = http_get(f"{base}/api/v1/health")
            if status == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        raise AssertionError("runtime never became healthy")

    yield base, proc

    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()


def test_stage8_heartbeat_runtime_e2e(
    runtime_with_slow_planner, tmp_path
):
    base, proc = runtime_with_slow_planner

    def git(repo, *args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    repo = tmp_path / "hb_e2e_target_repo"
    repo.mkdir()

    git(repo, "init")
    git(repo, "config", "user.email", "e2e@yodaw.local")
    git(repo, "config", "user.name", "YODAW E2E")
    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )
    (repo / "test_greet.py").write_text(
        "from greet import greet\n\n\n"
        "def test_greet():\n"
        "    assert greet('E2E') == 'Hello E2E'\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")

    # ----- POST returns QUEUED immediately -----
    t0 = time.monotonic()
    status, queued = http_post_json(
        f"{base}/api/v1/missions",
        {
            "goal": "Refactor greet(name) to use an f-string "
            "without changing behavior.",
            "capability": "repo-code",
            "metadata": {
                "repo_path": str(repo),
                "commit_message": "hb e2e: use f-string greeting",
            },
        },
    )
    post_latency = time.monotonic() - t0

    assert status == 200
    assert queued["status"] == "QUEUED"
    assert post_latency < 2.0, post_latency

    mission_id = queued["id"]

    # ----- Bounded QUEUED -> RUNNING/EXECUTING transition (slow schedulers may lag) -----
    transition_deadline = time.monotonic() + 15
    while True:
        status, mission = http_get(f"{base}/api/v1/missions/{mission_id}")
        assert status == 200
        if mission["status"] in ("RUNNING", "EXECUTING"):
            break
        if mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED"):
            raise AssertionError(
                "mission reached terminal "
                f"{mission['status']} without ever observing RUNNING/EXECUTING"
            )
        assert mission["status"] == "QUEUED", (
            f"unexpected pre-RUNNING status: {mission['status']}"
        )
        assert time.monotonic() < transition_deadline, (
            "mission never transitioned QUEUED -> RUNNING/EXECUTING within 15s; "
            f"last status={mission['status']}"
        )
        time.sleep(0.2)

    # ----- While RUNNING, the persisted heartbeat advances -----
    heartbeats = []
    health_latencies = []
    final = None
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        status, mission = http_get(f"{base}/api/v1/missions/{mission_id}")
        assert status == 200

        if mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED"):
            final = mission
            break

        assert mission["status"] in ("RUNNING", "EXECUTING"), mission["status"]

        heartbeats.append(mission["heartbeat_at"])

        h0 = time.monotonic()
        h_status, _ = http_get(f"{base}/api/v1/health")
        health_latencies.append(time.monotonic() - h0)
        assert h_status == 200

        time.sleep(0.5)

    assert final is not None and final["status"] == "PASS", final

    # The slow planner blocked the worker ~4s; the heartbeat (1s
    # cadence) must have advanced repeatedly during that window.
    assert len(heartbeats) >= 3, heartbeats
    assert len(set(heartbeats)) >= 3, "heartbeat must keep advancing"

    # API responsiveness held during the whole long call.
    assert max(health_latencies) < 2.0, max(health_latencies)

    # ----- Exactly one commit; source repo untouched -----
    result = final["result"]
    branch = result["branch"]

    assert git(repo, "rev-list", "--count", branch) == "2"
    assert git(repo, "rev-parse", "HEAD") == baseline

    # ----- Events + heartbeat evidence of the long call -----
    status, events = http_get(f"{base}/api/v1/missions/{mission_id}/events")
    event_types = [e["event_type"] for e in events["events"]]
    assert "mission.queued" in event_types
    assert "mission.started" in event_types
    assert "mission.completed" in event_types

    proc.send_signal(signal.SIGTERM)
    exit_code = proc.wait(timeout=20)
    assert exit_code == 0, exit_code
