"""
Worker A deliverable: YODAW launcher / runtime supervisor.

Integration tests drive the real CLI (`python -m app.launcher`),
which spawns the detached supervisor daemon and the real runtime
(`app.runtime`, embedded API + coordinator + watchdog + relay).
Each test runs on its own free port, temp DB, and runtime dir, so
tests never touch `data/yodaw.db`, port 8844, or any running
development instance.

Covered: fresh start, double-start safety, health, status, stop,
restart, crash recovery (restart for a failed service), and absence
of stray processes after shutdown.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _proxyless_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _health(url: str, timeout: float = 2.0) -> bool:
    try:
        with _proxyless_opener().open(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _process_command(pid: int) -> str:
    r = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


class Harness:
    """One isolated launcher instance (own port, DB, runtime dir)."""

    def __init__(self, tmp_path: Path):
        self.dir = Path(tmp_path)
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/api/v1/health"
        self.env = os.environ.copy()
        self.env.update(
            {
                "YODAW_PORT": str(self.port),
                "YODAW_DB_PATH": str(self.dir / "yodaw.db"),
                "YODAW_RUNTIME_DIR": str(self.dir / "rt"),
                "YODAW_PROFILE": "local",
                "YODAW_LOG_LEVEL": "WARNING",
            }
        )

    def run(self, *args: str, timeout: int = 240):
        return subprocess.run(
            [PY, "-m", "app.launcher", *args],
            cwd=str(REPO),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def start(self, timeout: int = 120):
        r = self.run("start", "--timeout", str(timeout))
        assert r.returncode == 0, f"start failed: {r.stderr} {r.stdout}"
        assert self.wait_healthy(), "runtime never became healthy"
        return r

    def wait_healthy(self, timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _health(self.url):
                return True
            time.sleep(0.5)
        return False

    def supervisor_pid(self) -> int:
        return int((self.dir / "rt" / "supervisor.pid").read_text().strip())

    def runtime_pid(self) -> int:
        return int(self.state()["services"]["runtime"]["pid"])

    def state(self) -> dict:
        return json.loads((self.dir / "rt" / "state.json").read_text())

    def pidfile_exists(self) -> bool:
        return (self.dir / "rt" / "supervisor.pid").exists()

    def stop(self, timeout: int = 120):
        r = self.run("stop", "--timeout", str(timeout))
        assert r.returncode == 0, f"stop failed: {r.stderr} {r.stdout}"
        return r

    def listeners_on_port(self) -> list:
        r = subprocess.run(
            ["lsof", "-iTCP:" + str(self.port), "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
        )
        return [ln for ln in r.stdout.split() if ln.isdigit()]

    def teardown(self):
        """Best-effort: graceful stop, then hard-kill any leftovers."""
        try:
            self.run("stop", "--timeout", "30")
        except Exception:
            pass
        try:
            state = self.state()
        except Exception:
            state = {}
        pids = [state.get("supervisor_pid")] + [
            svc.get("pid") for svc in state.get("services", {}).values()
        ]
        for pid in (p for p in pids if p):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + 10
        while time.time() < deadline:
            if not self.listeners_on_port():
                break
            time.sleep(0.5)


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    yield h
    h.teardown()


def test_fresh_start_health_status_and_stop(harness):
    r = harness.start()
    assert "started" in r.stdout

    # Health endpoint answers with READY.
    assert _health(harness.url), "health endpoint unreachable after start"

    # status reports the supervisor and a running runtime.
    r = harness.run("status")
    assert r.returncode == 0, r.stderr
    assert "UP" in r.stdout and "running" in r.stdout
    state = harness.state()
    assert state["services"]["runtime"]["status"] == "running"
    assert state["services"]["runtime"]["restarts"] == 0

    # stop: graceful, idempotent, leaves no files behind.
    r = harness.stop()
    assert "stopped" in r.stdout
    assert not harness.pidfile_exists(), "pidfile must be removed on stop"
    assert not (harness.dir / "rt" / "state.json").exists()

    r = harness.run("stop")
    assert r.returncode == 0, "stop of a stopped runtime must be a no-op"


def test_double_start_is_safe(harness):
    harness.start()
    spid = harness.supervisor_pid()
    rpid = harness.runtime_pid()

    # Second start must not create a second supervisor or runtime.
    r = harness.run("start")
    assert r.returncode == 0
    assert "already running" in r.stdout
    assert harness.supervisor_pid() == spid
    assert harness.runtime_pid() == rpid
    assert len(harness.listeners_on_port()) == 1, "exactly one process on port"

    harness.stop()
    assert not harness.pidfile_exists()


def test_health_command_and_json_status(harness):
    harness.start()

    r = harness.run("health")
    assert r.returncode == 0, r.stderr
    assert '"status": "READY"' in r.stdout

    r = harness.run("status", "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    assert doc["running"] is True
    assert doc["supervisor_pid"] == harness.supervisor_pid()
    assert doc["services"]["runtime"]["status"] == "running"
    assert doc["health"]["ok"] is True

    r = harness.run("logs", "-n", "5")
    assert r.returncode == 0
    assert "[supervisor]" in r.stdout, "supervisor log lines must be present"


def test_restart_replaces_processes(harness):
    harness.start()
    spid_old = harness.supervisor_pid()
    rpid_old = harness.runtime_pid()

    r = harness.run("restart")
    assert r.returncode == 0, r.stderr
    assert harness.wait_healthy(), "runtime not healthy after restart"

    spid_new = harness.supervisor_pid()
    rpid_new = harness.runtime_pid()
    assert spid_new != spid_old, "restart must start a new supervisor"
    assert rpid_new != rpid_old, "restart must start a new runtime"
    assert len(harness.listeners_on_port()) == 1, "exactly one runtime after restart"

    assert _process_command(rpid_old) == "", "old runtime process must be gone"

    harness.stop()


def test_crash_recovery_respawns_runtime(harness):
    harness.start()
    rpid = harness.runtime_pid()

    # Hard-kill the runtime; the supervisor must respawn it and the
    # service must return to healthy.
    os.kill(rpid, signal.SIGKILL)

    deadline = time.time() + 120
    while time.time() < deadline:
        state = harness.state()
        svc = state["services"]["runtime"]
        if (
            svc["status"] == "running"
            and svc["restarts"] >= 1
            and svc["pid"] != rpid
            and _health(harness.url)
        ):
            break
        time.sleep(1)
    else:
        pytest.fail(
            f"runtime not recovered; state={harness.state()!r}"
        )

    new_pid = harness.runtime_pid()
    assert new_pid != rpid
    assert _process_command(new_pid).strip(), "respawned runtime alive"
    assert harness.state()["services"]["runtime"]["restarts"] >= 1
    assert _health(harness.url), "health must pass after recovery"

    # Supervisor itself must still be the same singleton.
    assert harness.pidfile_exists()

    harness.stop()


def test_stop_leaves_no_stray_processes(harness):
    harness.start()
    pids = {harness.supervisor_pid(), harness.runtime_pid()}

    harness.stop()

    deadline = time.time() + 30
    while time.time() < deadline:
        if not harness.listeners_on_port():
            break
        time.sleep(0.5)
    assert harness.listeners_on_port() == [], "port still served after stop"

    for pid in pids:
        cmd = _process_command(pid)
        assert "app.runtime" not in cmd, f"stray process {pid} still alive: {cmd}"
        assert "app.launcher" not in cmd, f"stray process {pid} still alive: {cmd}"


def test_start_refuses_foreign_service_on_port(harness):
    """A runtime not owned by the launcher must block `start` (no
    duplicate processes) and itself be detected as already running."""

    class _FakeHealth(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"service":"YODAW","status":"READY","auth":"local-dev"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", harness.port), _FakeHealth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        r = harness.run("start")
        assert r.returncode == 1
        assert "already served by another process" in r.stderr
        assert not harness.pidfile_exists(), "no supervisor may start"
    finally:
        server.shutdown()
        server.server_close()