"""
Stage 8.13: hermetic runtime E2E.

Full mission lifecycle through the real runtime entrypoint
(python -m app.runtime, real HTTP, real SQLite, real coordinator,
real worker, real provider code path) with two hermetic fakes:

- a fake Ollama server that returns a canned structured plan, so
  the provider/retry/parse path is exercised without a model
- a disposable target git repository

Verifies the Stage 8 graduation contract:
POST -> QUEUED fast -> RUNNING -> PASS
- mission id stable
- events recorded (queued/started/completed)
- evidence recorded (llm_plan/edit/worktree_cleanup)
- learning record created
- commit created exactly once
- target source repo safe (HEAD at baseline)
- worktree removed after success
- API stays responsive while the mission executes
- SIGTERM shutdown clean
"""

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
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
    calls = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.calls.append(time.time())

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
def fake_ollama():
    server = HTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    FakeOllamaHandler.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", FakeOllamaHandler.calls
    server.shutdown()


def git(repo: Path, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@pytest.fixture()
def runtime_with_fake_planner(tmp_path, fake_ollama):
    fake_url, calls = fake_ollama

    project_root = Path(__file__).resolve().parents[1]

    env = dict(__import__("os").environ)
    env.update(
        {
            "YODAW_DB_PATH": str(tmp_path / "e2e.sqlite"),
            "YODAW_PORT": str(free_port()),
            "YODAW_LLM_STYLE": "ollama",
            "YODAW_LLM_BASE_URL": fake_url,
            "YODAW_LLM_MODEL": "fake-planner",
            "YODAW_LLM_TIMEOUT_SECONDS": "30",
            "YODAW_PROVIDER_MAX_RETRIES": "2",
            "YODAW_PROVIDER_BACKOFF_SECONDS": "0.05",
            "YODAW_ENABLE_GITHUB": "false",
            "YODAW_LOG_LEVEL": "WARNING",
        }
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.runtime"],
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
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

    yield base, proc, env, calls

    if proc.poll() is None:
        proc.send_signal(__import__("signal").SIGTERM)
        proc.wait(timeout=20)


def test_stage8_hermetic_runtime_e2e(runtime_with_fake_planner, tmp_path):
    base, proc, env, provider_calls = runtime_with_fake_planner

    # ----- Disposable target repository -----
    repo = tmp_path / "e2e_target_repo"
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

    # ----- POST returns QUEUED quickly -----
    t0 = time.monotonic()

    status, queued = http_post_json(
        f"{base}/api/v1/missions",
        {
            "goal": "Refactor greet(name) to use an f-string "
            "without changing behavior.",
            "capability": "repo-code",
            "metadata": {
                "repo_path": str(repo),
                "commit_message": "e2e: use f-string greeting",
            },
        },
    )

    post_latency = time.monotonic() - t0

    assert status == 200
    assert queued["status"] == "QUEUED", queued
    assert post_latency < 2.0, post_latency

    mission_id = queued["id"]

    # ----- API stays responsive while the mission executes -----
    health_latencies = []

    # ----- Poll through the state machine -----
    observed = []
    final = None
    deadline = time.monotonic() + 60

    while time.monotonic() < deadline:
        status, mission = http_get(f"{base}/api/v1/missions/{mission_id}")

        assert status == 200
        assert mission["id"] == mission_id, "mission id must be stable"

        if mission["status"] != (observed[-1] if observed else None):
            observed.append(mission["status"])

        h0 = time.monotonic()
        h_status, _ = http_get(f"{base}/api/v1/health")
        health_latencies.append(time.monotonic() - h0)

        assert h_status == 200, "health must stay responsive"

        if mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED"):
            final = mission
            break

        time.sleep(0.1)

    assert final is not None, observed
    assert final["status"] == "PASS", final

    # Observed transitions must show the async progression. The
    # POST response proved QUEUED; polling may legitimately catch
    # the mission already RUNNING (a fast coordinator claims
    # within milliseconds).
    assert observed[0] in ("QUEUED", "RUNNING")
    assert "RUNNING" in observed
    assert observed[-1] == "PASS"

    # The provider path really ran through the fake Ollama.
    assert len(provider_calls) >= 1

    result = final["result"]

    assert result["tests_passed"] is True
    assert result["retries"] == 0
    assert result["commit_sha"]

    # ----- Commit created exactly once; source repo safe -----
    branch = result["branch"]

    assert git(repo, "rev-list", "--count", branch) == "2"
    assert git(repo, "rev-parse", "HEAD") == baseline

    committed_code = git(repo, "show", f"{branch}:greet.py")

    assert "f'Hello {name}'" in committed_code

    # Worktree removed after success.
    assert not Path(result["worktree"]).exists()

    # ----- Events recorded -----
    status, events = http_get(f"{base}/api/v1/missions/{mission_id}/events")

    event_types = [e["event_type"] for e in events["events"]]

    for expected in (
        "mission.queued",
        "mission.started",
        "mission.completed",
    ):
        assert expected in event_types, event_types

    # ----- Evidence recorded -----
    status, evidence = http_get(
        f"{base}/api/v1/missions/{mission_id}/evidence"
    )

    evidence_types = {
        e.get("type") for e in evidence["evidence"] if isinstance(e, dict)
    }

    assert "llm_plan" in evidence_types
    assert "edit" in evidence_types
    assert "worktree_cleanup" in evidence_types

    # ----- Learning recorded -----
    status, learning = http_get(f"{base}/api/v1/learning")

    records = [
        r
        for r in learning
        if r.get("mission_id") == mission_id
    ]

    assert records, "learning record must exist"
    assert records[0]["outcome"] == "PASS"

    # ----- Clean shutdown -----
    import signal

    proc.send_signal(signal.SIGTERM)
    exit_code = proc.wait(timeout=20)

    assert exit_code == 0, exit_code

    # Health latency stayed comfortable throughout execution.
    assert max(health_latencies) < 1.0, max(health_latencies)
