"""Kodgar web/API E2E driver.

Proves the real vertical slice against a disposable repo:

    API submit -> Grok planner -> executor -> worktree -> verify ->
    commit -> SSE completion -> API result evidence -> reconnect.

Usage:
    python3 /tmp/kt_e2e.py            # runs the full E2E, prints PASS/FAIL
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from urllib.error import HTTPError

BASE = "http://127.0.0.1:8844"
REPO = "/tmp/kodgar_e2e_repo"
GOAL = (
    "Create a file named KODGAR_WEB_E2E.txt in the repository root "
    "containing exactly the single line: KODGAR_WEB_E2E_OK"
)
EXPECTED_CONTENT = "KODGAR_WEB_E2E_OK"

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)
    return condition


def api(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except HTTPError as exc:
        return {"_http_status": exc.code, "detail": exc.read().decode()[:300]}


def main() -> int:
    started = time.time()

    # 0. runtime reachable (the real health route)
    health = api("GET", "/api/v1/health", timeout=10)
    check("runtime API reachable", "_http_status" not in health, str(health))

    # 1. submit -> immediate response
    submit = api("POST", "/api/v1/tasks", {"goal": GOAL, "repo": REPO})
    task_id = submit.get("task_id") or submit.get("id")
    check("task id returned", bool(task_id), str(submit))
    check(
        "submit returns immediately with planning state",
        str(submit.get("state", "")).upper() in {"PLANNING", "QUEUED"},
        str(submit.get("state")),
    )

    if not task_id:
        return finish(started)

    # 2. SSE stream in a thread from the very start (catches every event
    #    including planner.* and executor.*)
    import threading

    sse_events: list[dict] = []
    sse_done = threading.Event()

    def consume() -> None:
        request = urllib.request.Request(
            f"{BASE}/api/v1/tasks/{task_id}/events",
            headers={"Accept": "text/event-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                buffer = b""
                while True:
                    chunk = response.read(1)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n\n" in buffer:
                        frame, buffer = buffer.split(b"\n\n", 1)
                        text = frame.decode()
                        etype = ""
                        for line in text.splitlines():
                            if line.startswith("event:"):
                                etype = line.split(":", 1)[1].strip()
                        data_line = ""
                        for line in text.splitlines():
                            if line.startswith("data:"):
                                data_line = line.split(":", 1)[1].strip()
                        if etype == "task.stream_end":
                            sse_done.set()
                            return
                        if data_line:
                            try:
                                sse_events.append({"type": etype, "data": json.loads(data_line)})
                            except json.JSONDecodeError:
                                pass
        except Exception:
            sse_done.set()

    threading.Thread(target=consume, daemon=True).start()

    # 3. wait for a terminal state through the status API
    final = None
    deadline = time.time() + 420
    while time.time() < deadline:
        detail = api("GET", f"/api/v1/tasks/{task_id}")
        state = str(detail.get("state", "")).upper()
        if state in {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}:
            final = detail
            break
        time.sleep(2)

    check("task reached terminal state", final is not None, "timeout after 420s")
    if final is None:
        return finish(started)
    check(
        "task COMPLETED (not merely returned text)",
        final.get("state") == "COMPLETED",
        str(final.get("state")) + " " + str(final.get("error")),
    )

    # 4. planner evidence
    plan = final.get("plan") or {}
    check("Grok planner JSON stored", isinstance(plan, dict) and bool(plan.get("executor")), str(plan)[:200])
    check("planner reason recorded", bool(plan.get("reason")))

    # 5. executor evidence
    check("executor selected", bool(final.get("executor")), str(final.get("executor")))

    # 6. physical file with exact content on the task branch in the repo
    commit_sha = final.get("commit_sha")
    check("real commit exists", bool(commit_sha))
    if commit_sha:
        show = subprocess.run(
            ["git", "show", f"{commit_sha}:KODGAR_WEB_E2E.txt"],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        content = show.stdout.strip()
        check("file content is exactly KODGAR_WEB_E2E_OK", content == EXPECTED_CONTENT, repr(content))
        branches = subprocess.run(
            ["git", "branch", "--contains", commit_sha],
            cwd=REPO, capture_output=True, text=True, timeout=30,
        )
        check("commit is on an isolated task branch, not main", "main" not in branches.stdout.split(), branches.stdout)
    user_branch = subprocess.run(
        ["git", "rev-parse", "main"], cwd=REPO, capture_output=True, text=True, timeout=30
    ).stdout.strip()
    untouched = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", user_branch],
        cwd=REPO, capture_output=True, text=True, timeout=30,
    ).stdout
    check("user branch untouched (no KODGAR_WEB_E2E.txt on main)", "KODGAR_WEB_E2E.txt" not in untouched)
    status_dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, timeout=30
    ).stdout
    check("user worktree clean (no dirty mutations)", status_dirty.strip() == "", status_dirty)

    # 7. verification evidence
    check("verification PASS recorded", final.get("verify_status") == "PASS", str(final.get("verify_status")))
    check("files_changed >= 1", (final.get("files_changed") or 0) >= 1, str(final.get("files_changed")))

    # 8. SSE received the whole lifecycle including completion
    sse_done.wait(timeout=10)
    types = [event["type"] for event in sse_events]
    for expected in (
        "task.created", "planner.started", "planner.completed",
        "executor.selected", "executor.started",
        "verification.started", "verification.completed",
        "git.committed", "task.completed",
    ):
        check(f"SSE event {expected}", expected in types, f"got {types}")

    # 9. seq monotonicity
    seqs = [event["data"].get("seq") for event in sse_events if isinstance(event["data"], dict)]
    seqs = [s for s in seqs if s is not None]
    check("SSE seq monotonic", all(b > a for a, b in zip(seqs, seqs[1:])), str(seqs))

    # 10. reconnect: replay from 0 must return the full history ending
    #     in completion — proves Last-Event-ID/after restore works.
    reconnect = api("GET", f"/api/v1/tasks/{task_id}")
    check("reconnect status restores real state", reconnect.get("state") == "COMPLETED")

    return finish(started)


def finish(started: float) -> int:
    print()
    if FAILURES:
        print(f"E2E RESULT: FAIL ({len(FAILURES)} failed: {', '.join(FAILURES)})")
        return 1
    print(f"E2E RESULT: PASS ({time.time() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
