#!/usr/bin/env python3.12
"""
YODAW API E2E Smoke Client (Worker E deliverable).

Black-box end-to-end test of the public HTTP API:
START SERVER -> READINESS -> SUBMIT TASK -> RECEIVE TASK ID ->
POLL STATUS -> RETRIEVE RESULT/EVIDENCE ->
RETRY OR CANCEL PATH -> TERMINAL STATE -> CLEAN SHUTDOWN

Usage:
    python3.12 scripts/e2e_smoke.py --base-url http://127.0.0.1:8844 --profile local

Exit codes:
    0 = full lifecycle PASS
    1 = any lifecycle stage FAIL
    2 = server startup FAIL
    3 = configuration error
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_HEADERS = {"Content-Type": "application/json"}


@dataclass
class Config:
    base_url: str
    profile: str
    api_key: str | None = None
    timeout: float = 5.0
    poll_interval: float = 0.5
    poll_timeout: float = 120.0
    startup_timeout: float = 20.0
    shutdown_timeout: float = 25.0
    start_server: bool = False


class APIError(Exception):
    def __init__(self, status: int, detail: str, path: str):
        self.status = status
        self.detail = detail
        self.path = path
        super().__init__(f"{path} -> {status}: {detail}")


class SmokeClient:
    """JSON-only HTTP client for the YODAW public API."""

    def __init__(self, base_url: str, timeout: float = 5.0, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = dict(DEFAULT_HEADERS)
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def _req(self, method: str, path: str, payload: dict | None = None) -> tuple[int, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self.headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            raise APIError(e.code, body, path)
        except Exception as e:
            raise APIError(0, str(e), path)

    def get(self, path: str) -> tuple[int, Any]:
        return self._req("GET", path)

    def post(self, path: str, payload: dict | None = None) -> tuple[int, Any]:
        return self._req("POST", path, payload)

    # ----- Public API surface -----

    def health(self) -> dict:
        _, body = self.get("/api/v1/health")
        return body

    def status(self) -> dict:
        _, body = self.get("/api/v1/status")
        return body

    def capabilities(self) -> dict:
        _, body = self.get("/api/v1/capabilities")
        return body

    def submit_mission(
        self,
        goal: str,
        capability: str = "code",
        **kwargs,
    ) -> dict:
        body = {"goal": goal, "capability": capability}
        body.update({k: v for k, v in kwargs.items() if v is not None})
        _, resp = self.post("/api/v1/missions", body)
        return resp

    def get_mission(self, mission_id: str) -> dict:
        _, resp = self.get(f"/api/v1/missions/{mission_id}")
        return resp

    def get_evidence(self, mission_id: str) -> dict:
        _, resp = self.get(f"/api/v1/missions/{mission_id}/evidence")
        return resp

    def get_events(self, mission_id: str) -> dict:
        _, resp = self.get(f"/api/v1/missions/{mission_id}/events")
        return resp

    def cancel_mission(self, mission_id: str) -> dict:
        _, resp = self.post(f"/api/v1/missions/{mission_id}/cancel")
        return resp

    def retry_mission(self, mission_id: str) -> dict:
        _, resp = self.post(f"/api/v1/missions/{mission_id}/retry")
        return resp

    def poll_to_terminal(
        self,
        mission_id: str,
        timeout: float,
        interval: float,
    ) -> dict:
        deadline = time.monotonic() + timeout
        last_status = None
        while time.monotonic() < deadline:
            resp = self.get_mission(mission_id)
            st = resp.get("status")
            if st != last_status:
                print(f"  [{mission_id[:12]}] status: {st}")
                last_status = st
            if st in ("PASS", "FAIL", "BLOCKED", "BLOCKED_EXTERNAL", "CANCELLED"):
                return resp
            time.sleep(interval)
        raise TimeoutError(
            f"mission {mission_id} did not reach terminal state within {timeout}s"
        )


def wait_for_ready(client: SmokeClient, timeout: float) -> dict:
    """Wait until /api/v1/health reports READY."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            body = client.health()
            if body.get("status") == "READY":
                return body
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"server not READY within {timeout}s")


def run_lifecycle_test(config: Config) -> int:
    """Execute the full lifecycle against an already-running server."""
    client = SmokeClient(config.base_url, config.timeout, config.api_key)

    # 1. READINESS
    print("== READINESS ==")
    health = wait_for_ready(client, config.startup_timeout)
    print(f"  health: {health.get('status')} profile={health.get('profile')} auth={health.get('auth')}")
    caps = client.capabilities()
    print(f"  capabilities: {[c['name'] for c in caps.get('capabilities', [])]}")

    # 2. SUBMIT TASK (deterministic code worker)
    print("\n== SUBMIT TASK ==")
    goal = "Add a multiply function to mathlib with tests"
    submit = client.submit_mission(goal=goal, capability="code")
    mission_id = submit.get("mission_id") or submit.get("id")
    assert mission_id, "no mission_id in submit response"
    print(f"  submitted: {mission_id} status={submit.get('status')}")

    # 3. POLL STATUS
    print("\n== POLL STATUS ==")
    final = client.poll_to_terminal(mission_id, config.poll_timeout, config.poll_interval)
    print(f"  terminal: {final.get('status')} worker={final.get('worker')} err_class={final.get('error_class')}")

    # 4. RETRIEVE EVIDENCE
    print("\n== EVIDENCE ==")
    ev = client.get_evidence(mission_id)
    evidence = ev.get("evidence", [])
    print(f"  evidence items: {len(evidence)}")
    for i, item in enumerate(evidence[:5]):
        t = item.get("type", item.get("cmd", "?"))
        print(f"    [{i}] {t[:80]}")
    if len(evidence) > 5:
        print(f"    ... +{len(evidence)-5} more")

    # 5. EVENTS
    print("\n== EVENTS ==")
    evts = client.get_events(mission_id)
    events = evts.get("events", [])
    print(f"  events: {len(events)}")

    # 5b. Verify terminal is PASS for code worker
    assert final.get("status") == "PASS", f"expected PASS, got {final.get('status')}"

    # 6. CANCEL PATH (queued mission)
    print("\n== CANCEL PATH (queued) ==")
    # submit a few to fill concurrency, then cancel the last
    ids = []
    for i in range(3):
        s = client.submit_mission(goal=f"cancel probe {i}", capability="code")
        ids.append(s["mission_id"])
    cancel_resp = client.cancel_mission(ids[-1])
    print(f"  cancel queued -> {cancel_resp.get('status')}")

    # 6b. Cancel finished -> 409
    print("\n== CANCEL PATH (finished) ==")
    try:
        client.cancel_mission(mission_id)
        print("  ERROR: expected 409")
        return 1
    except APIError as e:
        if e.status == 409:
            print(f"  cancel finished -> 409 OK: {e.detail[:80]}")
        else:
            raise

    # 7. RETRY PATH
    print("\n== RETRY PATH ==")
    # Retry PASS -> 409
    try:
        client.retry_mission(mission_id)
        print("  ERROR: retry PASS expected 409")
        return 1
    except APIError as e:
        if e.status == 409:
            print(f"  retry PASS -> 409 OK: {e.detail[:80]}")
        else:
            raise

    # Retry BLOCKED (unknown capability) -> new QUEUED mission
    print("\n  submit unknown capability...")
    blocked = client.submit_mission(goal="no such worker", capability="nope-xyz")
    bid = blocked.get("mission_id")
    print(f"  blocked mission: {bid} status={blocked.get('status')}")
    retry_resp = client.retry_mission(bid)
    assert retry_resp.get("status") == "QUEUED", "retry BLOCKED should yield QUEUED"
    assert "retried_from" in retry_resp
    print(f"  retry BLOCKED -> new mission {retry_resp.get('mission_id')} retried_from={retry_resp.get('retried_from')}")

    # 8. EDGE CASES
    print("\n== EDGE CASES ==")
    # Unknown mission -> 404
    try:
        client.get_mission("m_doesnotexist")
        print("  ERROR: unknown mission expected 404")
        return 1
    except APIError as e:
        if e.status == 404:
            print(f"  unknown mission -> 404 OK")
        else:
            raise

    # 9. CLEAN SHUTDOWN verification (server still alive)
    print("\n== CLEAN SHUTDOWN VERIFICATION ==")
    health2 = client.health()
    assert health2.get("status") == "READY", "server not READY after lifecycle"
    print("  server still healthy")

    print("\n=== ALL LIFECYCLE STAGES PASS ===")
    return 0


def start_server(config: Config) -> subprocess.Popen:
    """Start the runtime server as a subprocess."""
    # Extract port from base_url
    port = "8844"
    if config.base_url.startswith("http://"):
        parts = config.base_url.split(":")
        if len(parts) >= 3:
            port = parts[2].split("/")[0]

    env = os.environ.copy()
    env.update(
        {
            "YODAW_PROFILE": config.profile,
            "YODAW_LOG_LEVEL": "WARNING",
            "YODAW_PORT": port,
        }
    )
    if config.api_key:
        env["YODAW_API_KEY"] = config.api_key
    # Smoke hammers the API rapidly; keep governance on but raise the
    # ceiling unless the operator already set one explicitly.
    if "YODAW_RATE_LIMIT_RPM" not in env:
        env["YODAW_RATE_LIMIT_RPM"] = "1000"
    if "YODAW_RATE_LIMIT_BURST" not in env:
        env["YODAW_RATE_LIMIT_BURST"] = "100"
    # DB path for isolation
    import tempfile
    db_fd, db_path = tempfile.mkstemp(prefix="yodaw_smoke_", suffix=".db")
    os.close(db_fd)
    env["YODAW_DB_PATH"] = db_path
    os.unlink(db_path)  # sqlite will recreate

    proc = subprocess.Popen(
        [sys.executable, "-m", "app.runtime"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def parse_args() -> Config:
    p = argparse.ArgumentParser(description="YODAW API E2E Smoke Test")
    p.add_argument("--base-url", default="http://127.0.0.1:8844", help="API base URL")
    p.add_argument("--profile", default="local", choices=["local", "single-node", "multi-process", "production"])
    p.add_argument("--timeout", type=float, default=5.0, help="HTTP timeout")
    p.add_argument("--poll-interval", type=float, default=0.5, help="poll interval")
    p.add_argument("--poll-timeout", type=float, default=120.0, help="poll timeout")
    p.add_argument("--startup-timeout", type=float, default=20.0, help="server startup timeout")
    p.add_argument("--start-server", action="store_true", help="start server via python -m app.runtime")
    p.add_argument("--shutdown-timeout", type=float, default=25.0, help="server shutdown timeout")
    p.add_argument("--api-key", default=None, help="bearer key (also YODAW_API_KEY); required for single-node profile")
    args = p.parse_args()
    return Config(
        base_url=args.base_url,
        profile=args.profile,
        api_key=args.api_key or os.environ.get("YODAW_API_KEY"),
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        poll_timeout=args.poll_timeout,
        startup_timeout=args.startup_timeout,
        shutdown_timeout=args.shutdown_timeout,
    )


def main() -> int:
    config = parse_args()

    server_proc = None
    try:
        if config.base_url.startswith("http://") and "127.0.0.1" in config.base_url:
            print(f"== START SERVER (profile={config.profile}) ==")
            server_proc = start_server(config)
            print(f"  server pid: {server_proc.pid}")
            time.sleep(2.0)  # brief pause before readiness check

        rc = run_lifecycle_test(config)
        return rc
    except APIError as e:
        print(f"API ERROR: {e.path} -> {e.status}: {e.detail}")
        return 1
    except TimeoutError as e:
        print(f"TIMEOUT: {e}")
        return 1
    except Exception as e:
        print(f"UNEXPECTED ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if server_proc:
            print("\n== SHUTDOWN ==")
            if server_proc.poll() is None:
                server_proc.send_signal(signal.SIGTERM)
                try:
                    server_proc.wait(timeout=config.shutdown_timeout)
                    print("  server stopped cleanly")
                except subprocess.TimeoutExpired:
                    server_proc.kill()
                    print("  server killed after timeout")
                    return 1


if __name__ == "__main__":
    sys.exit(main())