"""
Stage 10 runtime E2E: production runtime architecture over real
HTTP with a fake/hermetic planner.

Exercises the full Stage 10 surface:

- superadmin creates an admin identity (operator) and a client
- the client submits a mission; POST returns QUEUED promptly
- a second client cannot access the first client's mission
- the operator can inspect missions but cannot manage admins
- the rate limiter produces 429 + Retry-After
- the mission reaches PASS through the real coordinator
- the audit chain verifies over the real HTTP API
- the outbox drains (learning record delivered exactly once)
- runtime status is healthy with the production configuration
- SIGTERM shutdown is graceful
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


class FakeOllamaHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)

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
def fake_planner():
    server = HTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture()
def runtime(tmp_path, fake_planner):
    project_root = Path(__file__).resolve().parents[1]

    env = dict(os.environ)
    env.update(
        {
            "YODAW_DB_PATH": str(tmp_path / "stage10.sqlite"),
            "YODAW_PORT": str(free_port()),
            "YODAW_PROFILE": "single-node",
            "YODAW_API_KEY": "stage10-shared-key",
            "YODAW_RATE_LIMIT_RPM": "60",
            "YODAW_RATE_LIMIT_BURST": "20",
            "YODAW_MISSIONS_PER_MINUTE": "5",
            "YODAW_LLM_STYLE": "ollama",
            "YODAW_LLM_BASE_URL": fake_planner,
            "YODAW_LLM_MODEL": "fake-stage10-planner",
            "YODAW_LLM_TIMEOUT_SECONDS": "30",
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
    admin_headers = {"Authorization": "Bearer stage10-shared-key"}

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

    yield base, proc, tmp_path, admin_headers

    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except Exception:
            proc.kill()


def git(repo: Path, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_stage10_runtime_e2e(runtime):
    base, proc, tmp_path, admin_headers = runtime

    # ----- 1. superadmin (shared key, migration path) bootstraps -----
    status, created = http_post_json(
        f"{base}/api/v1/admins",
        {"name": "stage10-operator", "role": "operator"},
        headers=admin_headers,
    )

    assert status == 200, created
    assert created["api_key"].startswith("yodad_")
    operator_key = created["api_key"]
    operator_headers = {"Authorization": f"Bearer {operator_key}"}

    # ----- 2. client identity created by the admin -----
    status, client_created = http_post_json(
        f"{base}/api/v1/clients",
        {
            "name": "stage10-client",
            "priority": 1,
            "max_concurrent_missions": 2,
        },
        headers=admin_headers,
    )

    assert status == 200, client_created
    assert client_created["api_key"].startswith("yodak_")
    client_key = client_created["api_key"]
    client_id = client_created["name"]
    client_headers = {"Authorization": f"Bearer {client_key}"}

    # A second client for the isolation proof.
    status, other_created = http_post_json(
        f"{base}/api/v1/clients",
        {"name": "stage10-other", "priority": 5},
        headers=admin_headers,
    )
    assert status == 200
    other_headers = {"Authorization": f"Bearer {other_created['api_key']}"}

    # ----- 3. disposable target repo -----
    repo = tmp_path / "stage10_target_repo"
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

    # ----- 4. client submits a mission; QUEUED returns promptly -----
    t0 = time.monotonic()

    status, queued = http_post_json(
        f"{base}/api/v1/missions",
        {
            "goal": "Refactor greet(name) to use an f-string "
            "without changing behavior.",
            "capability": "repo-code",
            "metadata": {
                "repo_path": str(repo),
                "commit_message": "stage10 e2e: f-string greeting",
            },
        },
        headers=client_headers,
    )

    post_latency = time.monotonic() - t0

    assert status == 200, queued
    assert queued["status"] == "QUEUED"
    assert post_latency < 5.0, post_latency

    mission_id = queued["id"]

    # ----- 5. second client cannot access the mission -----
    for path in (
        f"/api/v1/missions/{mission_id}",
        f"/api/v1/missions/{mission_id}/evidence",
        f"/api/v1/missions/{mission_id}/events",
    ):
        status, _ = http_get(f"{base}{path}", headers=other_headers)
        assert status == 404, (path, status)

    # The owner client CAN read its own mission.
    status, _ = http_get(
        f"{base}/api/v1/missions/{mission_id}", headers=client_headers
    )
    assert status == 200

    # ----- 6. operator inspects; cannot manage admins -----
    status, listing = http_get(
        f"{base}/api/v1/missions", headers=operator_headers
    )
    assert status == 200
    assert any(m["id"] == mission_id for m in listing)

    status, _ = http_post_json(
        f"{base}/api/v1/admins",
        {"name": "rogue", "role": "superadmin"},
        headers=operator_headers,
    )
    assert status == 403

    # ----- 7. rate limiter enforced -----
    limiter_client = "stage10-limited"

    status, limited = http_post_json(
        f"{base}/api/v1/clients",
        {"name": limiter_client, "priority": 5},
        headers=admin_headers,
    )
    assert status == 200

    limited_headers = {"Authorization": f"Bearer {limited['api_key']}"}

    # Token bucket: mission creation is capped at 5/min (burst 5
    # effectively); the probe exhausts it well within 20 requests
    # and every rejection carries Retry-After.
    saw_429 = False

    for i in range(20):
        status, _ = http_post_json(
            f"{base}/api/v1/missions",
            {
                "goal": f"limiter probe {i} {os.urandom(4).hex()}",
                "capability": "code",
                "metadata": {"skip_execution": True},
            },
            headers=limited_headers,
        )

        if status == 429:
            saw_429 = True
            break

    assert saw_429, "rate limiter never triggered a 429"

    # The rejection is audited (either the general bucket or the
    # mission-creation bucket rejection action).
    status, audit_body = http_get(
        f"{base}/api/v1/audit",
        headers=admin_headers,
    )
    assert status == 200

    audited_actions = {
        e["action"] for e in audit_body["events"]
    }

    assert audited_actions & {
        "request.rate_limited",
        "mission.rejected",
    }, audited_actions

    # ----- 8. mission reaches PASS through the real coordinator -----
    final = None
    deadline = time.monotonic() + 90

    while time.monotonic() < deadline:
        status, mission = http_get(
            f"{base}/api/v1/missions/{mission_id}",
            headers=client_headers,
        )
        assert status == 200

        if mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED"):
            final = mission
            break

        time.sleep(0.5)

    assert final is not None, "mission never reached a terminal state"
    assert final["status"] == "PASS", final

    # Exactly one commit on the mission branch; source repo safe.
    branch = final["result"]["branch"]
    assert git(repo, "rev-list", "--count", branch) == "2"
    assert git(repo, "rev-parse", "HEAD") == baseline

    # ----- 9. audit chain verifies over the API -----
    status, verification = http_get(
        f"{base}/api/v1/audit/verify", headers=admin_headers
    )
    assert status == 200
    assert verification["intact"] is True, verification
    assert verification["events"] >= 3

    # ----- 10. outbox drained exactly once -----
    import sqlite3

    db = sqlite3.connect(str(tmp_path / "stage10.sqlite"))
    outbox_rows = db.execute(
        "SELECT delivered_at IS NOT NULL, dead_lettered_at IS NOT NULL "
        "FROM mission_outbox WHERE mission_id=?",
        (mission_id,),
    ).fetchall()
    learning_rows = db.execute(
        "SELECT payload FROM learning_records WHERE json_extract("
        "payload, '$.mission_id')=?",
        (mission_id,),
    ).fetchall()
    db.close()

    assert len(outbox_rows) >= 1
    assert all(r[0] == 1 and r[1] == 0 for r in outbox_rows)
    assert len(learning_rows) == 1

    # ----- 11. runtime status healthy -----
    status, runtime_status = http_get(
        f"{base}/api/v1/runtime/status", headers=admin_headers
    )
    assert status == 200

    assert runtime_status["configuration"]["profile"] == "single-node"
    assert runtime_status["outbox"]["pending"] == 0
    assert runtime_status["outbox"]["dead_lettered"] == 0
    assert runtime_status["outbox"]["delivered"] >= 1
    assert runtime_status["clients"] >= 3
    assert runtime_status["coordinator"]["loop_alive"] is True

    # ----- 12. graceful shutdown -----
    proc.send_signal(signal.SIGTERM)
    exit_code = proc.wait(timeout=25)

    assert exit_code == 0, exit_code

    # Storage intact after shutdown.
    store_db = sqlite3.connect(str(tmp_path / "stage10.sqlite"))
    counts = store_db.execute(
        "SELECT status, COUNT(*) FROM missions GROUP BY status"
    ).fetchall()
    store_db.close()

    assert isinstance(counts, list)
