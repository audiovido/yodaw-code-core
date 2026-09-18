"""CLI client for the Kodgar Task API.

The CLI stops being a second execution engine here: ``kodgar task``
talks to the same Task API the web UI uses, over HTTP, and prints the
same live event stream. One backend, two interfaces.

    kodgar task submit "fix the flaky login test" --repo .
    kodgar task status <task_id>
    kodgar task watch  <task_id>
    kodgar task list
    kodgar task cancel <task_id>
    kodgar task diff   <task_id>

Everything is read from the real API: no local state, no duplicated
lifecycle. ``--api`` (or ``KODGAR_API_URL``) points at the runtime;
the default matches the launcher's default host/port.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

EXIT_OK = 0
EXIT_TASK_FAILURE = 1
EXIT_USAGE = 2

DEFAULT_API = "http://127.0.0.1:8844"

TERMINAL = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED"}


def api_base(override: Optional[str] = None) -> str:
    base = (
        override
        or os.environ.get("KODGAR_API_URL")
        or os.environ.get("YODAW_API_URL")
        or DEFAULT_API
    )
    return base.rstrip("/")


def auth_headers() -> dict[str, str]:
    key = os.environ.get("YODAW_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _client():
    import httpx

    return httpx


def submit(
    goal: str,
    repo: Optional[str],
    api: Optional[str] = None,
    executor: str = "auto",
    project_id: Optional[str] = None,
    as_json: bool = False,
) -> tuple[int, Optional[str]]:
    httpx = _client()
    payload: dict[str, Any] = {
        "goal": goal,
        "preferences": {"executor": executor},
    }
    if repo:
        payload["repo"] = str(repo)
    if project_id:
        payload["project_id"] = project_id

    try:
        response = httpx.post(
            f"{api_base(api)}/api/v1/tasks",
            json=payload,
            headers=auth_headers(),
            timeout=30,
        )
    except Exception as exc:
        print(f"cannot reach the Kodgar Task API: {exc}", file=sys.stderr)
        return EXIT_USAGE, None

    if response.status_code not in (200, 202):
        print(
            f"submit failed ({response.status_code}): {response.text[:400]}",
            file=sys.stderr,
        )
        return EXIT_USAGE, None

    body = response.json()
    task_id = body.get("task_id") or body.get("id")
    if as_json:
        print(json.dumps(body, indent=2))
    else:
        print(f"task {task_id} accepted ({body.get('state')})")
        print(f"watch it: kodgar task watch {task_id}")
    return EXIT_OK, task_id


def status(task_id: str, api: Optional[str] = None, as_json: bool = False) -> int:
    httpx = _client()
    response = httpx.get(
        f"{api_base(api)}/api/v1/tasks/{task_id}",
        headers=auth_headers(),
        timeout=30,
    )
    if response.status_code != 200:
        print(f"({response.status_code}) {response.text[:300]}", file=sys.stderr)
        return EXIT_USAGE
    task = response.json()
    if as_json:
        print(json.dumps(task, indent=2))
        return EXIT_OK
    print(render_status(task))
    return EXIT_OK if task["state"] == "COMPLETED" else EXIT_TASK_FAILURE


def render_status(task: dict) -> str:
    lines = [
        f"task       {task['id']}",
        f"state      {task['state']}  ({task['progress']}%)",
        f"step       {task.get('current_step')}",
        f"planner    {task.get('planner')} ({task.get('planner_backend') or 'n/a'})",
        f"executor   {task.get('executor') or '-'}",
        f"repo       {task.get('repo')}",
        f"branch     {task.get('branch') or '-'}",
        f"commit     {task.get('commit_sha') or '-'}",
        f"files      {task.get('files_changed')}",
        f"tests      {task['tests']['passed']} passed / "
        f"{task['tests']['failed']} failed",
        f"build      {task.get('build_status')}",
        f"verify     {task.get('verify_status')}",
        f"elapsed    {task.get('elapsed_seconds')}s",
    ]
    if task.get("error"):
        lines.append(f"error      {json.dumps(task['error'])[:400]}")
    return "\n".join(lines)


def list_tasks(api: Optional[str] = None, as_json: bool = False, limit: int = 20) -> int:
    httpx = _client()
    response = httpx.get(
        f"{api_base(api)}/api/v1/tasks",
        params={"limit": limit},
        headers=auth_headers(),
        timeout=30,
    )
    if response.status_code != 200:
        print(f"({response.status_code}) {response.text[:300]}", file=sys.stderr)
        return EXIT_USAGE
    body = response.json()
    if as_json:
        print(json.dumps(body, indent=2))
        return EXIT_OK
    tasks = body.get("tasks") or []
    if not tasks:
        print("no tasks")
        return EXIT_OK
    for task in tasks:
        print(
            f"{task['id']}  {task['state']:<10} {task['progress']:>3}%  "
            f"{(task.get('executor') or '-') :<14} {task['goal'][:60]}"
        )
    return EXIT_OK


def cancel(task_id: str, api: Optional[str] = None) -> int:
    httpx = _client()
    response = httpx.post(
        f"{api_base(api)}/api/v1/tasks/{task_id}/cancel",
        headers=auth_headers(),
        timeout=30,
    )
    print(f"({response.status_code}) {response.text[:300]}")
    return EXIT_OK if response.status_code == 200 else EXIT_TASK_FAILURE


def diff(task_id: str, api: Optional[str] = None) -> int:
    httpx = _client()
    response = httpx.get(
        f"{api_base(api)}/api/v1/tasks/{task_id}/diff",
        headers=auth_headers(),
        timeout=60,
    )
    if response.status_code != 200:
        print(f"({response.status_code}) {response.text[:300]}", file=sys.stderr)
        return EXIT_USAGE
    body = response.json()
    print(body.get("diff") or "(no diff recorded)")
    return EXIT_OK


def watch(
    task_id: str,
    api: Optional[str] = None,
    timeout: float = 3600.0,
    as_json: bool = False,
) -> int:
    """Stream a task's real events until it reaches a terminal state."""
    httpx = _client()
    base = api_base(api)
    url = f"{base}/api/v1/tasks/{task_id}/events"
    deadline = time.monotonic() + timeout
    final_state: Optional[str] = None

    try:
        with httpx.stream(
            "GET",
            url,
            headers=auth_headers(),
            timeout=httpx.Timeout(30.0, read=None),
        ) as response:
            if response.status_code != 200:
                print(
                    f"({response.status_code}) stream unavailable", file=sys.stderr
                )
                return EXIT_USAGE
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    print("watch timed out", file=sys.stderr)
                    return EXIT_TASK_FAILURE
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if as_json:
                    print(json.dumps(event))
                else:
                    print(f"[{event['seq']:>3}] {event['type']}")
                if event["type"] in {
                    "task.completed",
                    "task.failed",
                    "task.cancelled",
                    "task.blocked",
                }:
                    payload = event.get("data") or {}
                    final_state = payload.get("state") or event["type"].rsplit(".", 1)[-1]
                    break
    except Exception as exc:
        print(f"stream error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if final_state and final_state.upper() in TERMINAL:
        print(f"final state: {final_state.upper()}")
        return EXIT_OK if final_state.upper() == "COMPLETED" else EXIT_TASK_FAILURE
    print("stream ended without a terminal state", file=sys.stderr)
    return EXIT_TASK_FAILURE
