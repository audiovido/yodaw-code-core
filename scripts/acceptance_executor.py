"""Real acceptance driver for the Kodgar executor health/fallback layer.

Runs the Phase M scenarios against a LIVE API and prints physical
evidence (task state, chosen executor, attempt history, verification
checks, commit SHA). Nothing here fakes a result: every field printed
comes from the API, and the final verdict is derived from the task's
own terminal state.

Usage:
    python3 scripts/acceptance_executor.py health
    python3 scripts/acceptance_executor.py submit --goal "..." --repo /path
    python3 scripts/acceptance_executor.py wait <task_id>
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8844"
TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as response:
        return json.load(response)


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:  # surface the real API error
        raise SystemExit(f"POST {path} -> {exc.code}: {exc.read().decode()}")


def cmd_health() -> int:
    payload = _get("/api/v1/executors")
    executors = payload.get("executors") or {}
    print(f"{'id':<16}{'installed':<11}{'auth':<7}{'healthy':<9}"
          f"{'eligible':<10}{'error_type'}")
    for ident, info in sorted(executors.items()):
        print(
            f"{ident:<16}{str(info.get('installed')):<11}"
            f"{str(info.get('authenticated')):<7}"
            f"{str(info.get('healthy')):<9}{str(info.get('eligible')):<10}"
            f"{info.get('error_type')}"
        )
    eligible = [k for k, v in executors.items() if v.get("eligible")]
    print(f"\neligible_for_routing={eligible}")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    body = {
        "goal": args.goal,
        "repo": args.repo,
        "preferences": {"executor": args.executor},
        "timeout_seconds": args.timeout,
    }
    created = _post("/api/v1/tasks", body)
    print(json.dumps(created, indent=2))
    return 0


def _evidence(task: dict) -> dict:
    result = task.get("result") or {}
    # The API exposes the verifier report at the top level; tolerate the
    # nested shape too so this driver works against either.
    verification = task.get("verification") or result.get("verification") or {}
    return {
        "state": task.get("state"),
        "executor": task.get("executor"),
        "planner_backend": task.get("planner_backend")
        or (result.get("planner_attempts") or [{}])[-1],
        "attempts": result.get("executor_attempts"),
        "verification_status": verification.get("status"),
        "tests": task.get("tests"),
        "build": task.get("build_status"),
        "checks": [
            {
                "name": c.get("name"),
                "status": c.get("status"),
                "detail": (c.get("detail") or "")[:160],
            }
            for c in (verification.get("checks") or [])
        ],
        "commit": task.get("commit_sha") or result.get("commit"),
        "branch": task.get("branch"),
        "files_changed": task.get("files_changed"),
        "touched_files": task.get("touched_files"),
        "error": task.get("error"),
    }


def cmd_wait(args: argparse.Namespace) -> int:
    deadline = time.time() + args.timeout
    task: dict = {}
    while time.time() < deadline:
        task = _get(f"/api/v1/tasks/{args.task_id}")
        state = task.get("state")
        print(
            f"[{time.strftime('%H:%M:%S')}] {state:<12}"
            f"progress={task.get('progress')} step={task.get('current_step')}"
        )
        if state in TERMINAL:
            break
        time.sleep(args.poll)
    print("\n=== EVIDENCE ===")
    print(json.dumps(_evidence(task), indent=2))
    if task.get("state") != "COMPLETED" and not args.allow_failure:
        print(f"\nRESULT: NOT COMPLETED ({task.get('state')})")
        return 1
    print(f"\nRESULT: {task.get('state')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")

    submit = sub.add_parser("submit")
    submit.add_argument("--goal", required=True)
    submit.add_argument("--repo", required=True)
    submit.add_argument("--executor", default="auto")
    submit.add_argument("--timeout", type=int, default=3600)

    wait = sub.add_parser("wait")
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=float, default=2400)
    wait.add_argument("--poll", type=float, default=15)
    wait.add_argument("--allow-failure", action="store_true")

    args = parser.parse_args()
    if args.command == "health":
        return cmd_health()
    if args.command == "submit":
        return cmd_submit(args)
    return cmd_wait(args)


if __name__ == "__main__":
    sys.exit(main())
