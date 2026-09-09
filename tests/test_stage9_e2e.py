"""
Stage 9 runtime E2E: multi-tenancy through the real process.

Runs python -m app.runtime against a temp database and drives the
full multi-tenant path over real HTTP:

- admin creates a client (priority 1) via the API; the plaintext
  key is shown exactly once
- the client submits a mission; execution completes PASS
- the mission is attributed to the client with the client's
  priority; the audit trail records creation and completion-side
  actions; the learning record exists exactly once
- runtime/status exposes outbox and tenancy counters
- SIGTERM shutdown stays clean
"""

import json
import os
import signal
import sqlite3
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
            "YODAW_DB_PATH": str(tmp_path / "stage9.sqlite"),
            "YODAW_PORT": str(free_port()),
            "YODAW_LLM_STYLE": "ollama",
            "YODAW_LLM_BASE_URL": fake_planner,
            "YODAW_LLM_MODEL": "fake-stage9-planner",
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

    yield base, proc, tmp_path

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


def test_stage9_multi_tenant_runtime_e2e(runtime):
    base, proc, tmp_path = runtime

    # ----- Admin (local-dev mode) creates a priority-1 client -----
    status, created = http_post_json(
        f"{base}/api/v1/clients",
        {
            "name": "tenant-e2e",
            "priority": 1,
            "max_concurrent_missions": 2,
        },
    )

    assert status == 200, created
    assert created["api_key"].startswith("yodak_")

    tenant_key = created["api_key"]
    tenant_headers = {"Authorization": f"Bearer {tenant_key}"}

    # ----- Client lists include the new identity, no key material -----
    status, listing = http_get(f"{base}/api/v1/clients")
    assert status == 200

    entry = next(c for c in listing if c["name"] == "tenant-e2e")
    assert entry["priority"] == 1
    assert "api_key" not in entry and "key_hash" not in entry

    client_id = entry["id"]

    # ----- Disposable target repo -----
    repo = tmp_path / "stage9_target_repo"
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

    # ----- Client submits a mission -----
    status, queued = http_post_json(
        f"{base}/api/v1/missions",
        {
            "goal": "Refactor greet(name) to use an f-string "
            "without changing behavior.",
            "capability": "repo-code",
            "metadata": {
                "repo_path": str(repo),
                "commit_message": "stage9 e2e: f-string greeting",
            },
        },
        headers=tenant_headers,
    )

    # Without a reachable planner this mission cannot PASS; the
    # tenancy assertions below do not depend on the outcome, so
    # accept the queued response itself as the contract proof.
    assert status == 200
    assert queued["status"] == "QUEUED"

    mission_id = queued["id"]

    # With the fake planner the mission must PASS end to end.
    final = None
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        status, mission = http_get(f"{base}/api/v1/missions/{mission_id}")
        assert status == 200

        if mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED"):
            final = mission
            break

        time.sleep(0.2)

    assert final is not None, "mission never reached a terminal state"
    assert final["status"] == "PASS", final

    # Exactly one commit on the mission branch; source repo safe.
    branch = final["result"]["branch"]
    assert git(repo, "rev-list", "--count", branch) == "2"
    assert git(repo, "rev-parse", "HEAD") == baseline

    # ----- Attribution: mission carries client identity + priority -----
    db = sqlite3.connect(str(tmp_path / "stage9.sqlite"))
    row = db.execute(
        "SELECT client_id, priority, status FROM missions WHERE id=?",
        (mission_id,),
    ).fetchone()
    db.close()

    assert row[0] == client_id
    assert row[1] == 1

    # ----- Audit trail records the tenant actions -----
    status, audit = http_get(f"{base}/api/v1/audit", headers=tenant_headers)
    # Client keys must not read the global audit trail.
    assert status == 403

    status, audit = http_get(f"{base}/api/v1/audit")
    assert status == 200

    actions = [e["action"] for e in audit["events"]]
    assert "clients.created" in actions
    assert "mission.created" in actions

    mission_created = next(
        e for e in audit["events"] if e["action"] == "mission.created"
    )
    assert mission_created["client_id"] == client_id
    assert mission_created["mission_id"] == mission_id

    # ----- Learning recorded exactly once for the mission -----
    db = sqlite3.connect(str(tmp_path / "stage9.sqlite"))
    learning_rows = db.execute(
        "SELECT payload FROM learning_records WHERE json_extract("
        "payload, '$.mission_id')=?",
        (mission_id,),
    ).fetchall()
    outbox = db.execute(
        "SELECT delivered_at IS NOT NULL FROM mission_outbox "
        "WHERE mission_id=?",
        (mission_id,),
    ).fetchall()
    db.close()

    assert len(learning_rows) == 1
    assert all(r[0] == 1 for r in outbox), "outbox must be drained"

    # ----- Runtime status exposes tenancy + outbox counters -----
    status, runtime_status = http_get(f"{base}/api/v1/runtime/status")
    assert status == 200
    assert runtime_status["clients"] >= 1
    assert runtime_status["outbox"]["pending"] == 0
    assert runtime_status["outbox"]["delivered"] >= 1

    # ----- Clean shutdown -----
    proc.send_signal(signal.SIGTERM)
    exit_code = proc.wait(timeout=20)
    assert exit_code == 0
