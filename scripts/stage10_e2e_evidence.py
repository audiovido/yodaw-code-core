"""
Stage 10 E2E evidence capture: runs the production runtime
process exactly like tests/test_stage10_e2e.py, then prints the
concrete identifiers and states required by the graduation report
(admin id, client ids, mission id, transitions, isolation proof,
rate-limit proof, audit verification, outbox state, shutdown
result). Hermetic: fake planner, real coordinator.
"""

import json
import os
import signal
import threading
import sqlite3
import subprocess
import sys
import tempfile
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))
from test_runtime_service import free_port, http_get, http_post_json  # noqa: E402

PLAN = {
    "action": "edit",
    "edits": [
        {
            "target_file": "greet.py",
            "find": "return 'Hello ' + name",
            "replace": "return f'Hello {name}'",
        }
    ],
    "reason": "f-string",
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


def git(repo: Path, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="yodaw_e2e_evidence_"))
    planner = HTTPServer(("127.0.0.1", 0), FakeOllamaHandler)
    threading.Thread(target=planner.serve_forever, daemon=True).start()

    env = dict(os.environ)
    env.update(
        {
            "YODAW_DB_PATH": str(tmp / "stage10.sqlite"),
            "YODAW_PORT": str(free_port()),
            "YODAW_PROFILE": "single-node",
            "YODAW_API_KEY": "stage10-shared-key",
            "YODAW_RATE_LIMIT_RPM": "60",
            "YODAW_RATE_LIMIT_BURST": "20",
            "YODAW_MISSIONS_PER_MINUTE": "5",
            "YODAW_LLM_STYLE": "ollama",
            "YODAW_LLM_BASE_URL": f"http://127.0.0.1:{planner.server_port}",
            "YODAW_LLM_MODEL": "fake-evidence-planner",
            "YODAW_LLM_TIMEOUT_SECONDS": "30",
            "YODAW_ENABLE_GITHUB": "false",
            "YODAW_LOG_LEVEL": "WARNING",
        }
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.runtime"],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{env['YODAW_PORT']}"
    admin = {"Authorization": "Bearer stage10-shared-key"}

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if http_get(f"{base}/api/v1/health")[0] == 200:
                break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        print("E2E evidence: runtime never healthy")
        return 1

    evidence = {}

    # 1. admin identity (operator)
    status, operator = http_post_json(
        f"{base}/api/v1/admins",
        {"name": "evidence-operator", "role": "operator"},
        headers=admin,
    )
    assert status == 200
    evidence["admin_id"] = {
        "name": operator["name"],
        "role": "operator",
        "key_prefix": operator["api_key"][:8],
    }

    # 2. client identities
    status, client = http_post_json(
        f"{base}/api/v1/clients",
        {"name": "evidence-client", "priority": 1,
         "max_concurrent_missions": 2},
        headers=admin,
    )
    assert status == 200
    status, other = http_post_json(
        f"{base}/api/v1/clients",
        {"name": "evidence-other", "priority": 5},
        headers=admin,
    )
    assert status == 200
    evidence["client_ids"] = [client["name"], other["name"]]

    client_headers = {"Authorization": f"Bearer {client['api_key']}"}
    other_headers = {"Authorization": f"Bearer {other['api_key']}"}

    # 3. mission
    repo = tmp / "target"
    repo.mkdir()
    for args in (
        ["init"], ["config", "user.email", "e@y.local"],
        ["config", "user.name", "E"],
    ):
        git(repo, *args)
    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )
    (repo / "test_greet.py").write_text(
        "from greet import greet\n\n\n"
        "def test_greet():\n    assert greet('E2E') == 'Hello E2E'\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")

    status, queued = http_post_json(
        f"{base}/api/v1/missions",
        {
            "goal": "Refactor greet(name) to use an f-string "
            "without changing behavior.",
            "capability": "repo-code",
            "metadata": {
                "repo_path": str(repo),
                "commit_message": "evidence: f-string",
            },
        },
        headers=client_headers,
    )
    assert status == 200 and queued["status"] == "QUEUED"
    mission_id = queued["id"]
    evidence["mission_id"] = mission_id

    # 4. isolation proof
    iso = {}
    for path in (
        f"/api/v1/missions/{mission_id}",
        f"/api/v1/missions/{mission_id}/evidence",
        f"/api/v1/missions/{mission_id}/events",
    ):
        iso[path.split("/")[-1]] = http_get(
            f"{base}{path}", headers=other_headers
        )[0]
    evidence["second_client_access"] = iso  # all 404

    # 5. rate-limit proof (mission creation bucket: 5/min)
    limited = http_post_json(
        f"{base}/api/v1/clients",
        {"name": "evidence-limited", "priority": 5},
        headers=admin,
    )[1]
    limited_headers = {"Authorization": f"Bearer {limited['api_key']}"}
    codes = []
    for i in range(20):
        code, _ = http_post_json(
            f"{base}/api/v1/missions",
            {
                "goal": f"evidence probe {i} {os.urandom(4).hex()}",
                "capability": "code",
            },
            headers=limited_headers,
        )
        codes.append(code)
        if code == 429:
            break
    evidence["rate_limit"] = {"codes_seen": codes}

    # 6. operator inspect / cannot manage admins
    code, _ = http_get(f"{base}/api/v1/missions", headers={
        "Authorization": f"Bearer {operator['api_key']}"})
    operator_inspect = code
    code, _ = http_post_json(
        f"{base}/api/v1/admins",
        {"name": "rogue", "role": "superadmin"},
        headers={"Authorization": f"Bearer {operator['api_key']}"},
    )
    evidence["operator"] = {"inspect_missions": operator_inspect,
                            "create_admin": code}

    # 7. mission to PASS + state transitions
    transitions = []
    final = None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        code, mission = http_get(
            f"{base}/api/v1/missions/{mission_id}",
            headers=client_headers,
        )
        if mission["status"] not in transitions:
            transitions.append(mission["status"])
        if mission["status"] in ("PASS", "FAIL", "BLOCKED", "CANCELLED"):
            final = mission
            break
        time.sleep(0.3)

    assert final and final["status"] == "PASS", final
    branch = final["result"]["branch"]
    evidence["state_transitions"] = transitions
    evidence["commit_sha"] = git(repo, "rev-parse", branch)
    evidence["source_repo_untouched"] = (
        git(repo, "rev-parse", "HEAD") == baseline
    )
    evidence["commits_on_branch"] = git(repo, "rev-list", "--count", branch)

    # 8. audit verification
    code, verification = http_get(
        f"{base}/api/v1/audit/verify", headers=admin
    )
    evidence["audit_verification"] = verification

    # 9. outbox state
    db = sqlite3.connect(str(tmp / "stage10.sqlite"))
    outbox = db.execute(
        "SELECT delivered_at IS NOT NULL, dead_lettered_at IS NOT NULL "
        "FROM mission_outbox WHERE mission_id=?",
        (mission_id,),
    ).fetchall()
    learning = db.execute(
        "SELECT COUNT(*) FROM learning_records WHERE json_extract("
        "payload, '$.mission_id')=?",
        (mission_id,),
    ).fetchone()[0]
    db.close()
    evidence["outbox"] = {
        "rows_delivered": [bool(r[0]) for r in outbox],
        "dead_lettered": [bool(r[1]) for r in outbox],
        "learning_records": learning,
    }

    # 10. runtime status
    code, runtime_status = http_get(
        f"{base}/api/v1/runtime/status", headers=admin
    )
    evidence["runtime_status"] = {
        "profile": runtime_status["configuration"]["profile"],
        "outbox_pending": runtime_status["outbox"]["pending"],
        "coordinator_loop_alive": (
            runtime_status["coordinator"]["loop_alive"]
        ),
        "clients": runtime_status["clients"],
    }

    # 11. graceful shutdown
    proc.send_signal(signal.SIGTERM)
    exit_code = proc.wait(timeout=25)
    evidence["shutdown"] = {"sigterm_exit_code": exit_code}

    planner.shutdown()

    print(json.dumps(evidence, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
