"""Task API: the HTTP surface the web UI and the CLI both use.

    POST   /api/v1/tasks                    -> {task_id, state: PLANNING}
    GET    /api/v1/tasks                    -> list (live task list screen)
    GET    /api/v1/tasks/{id}               -> real state, real progress
    GET    /api/v1/tasks/{id}/events        -> SSE live stream (reconnectable)
    GET    /api/v1/tasks/{id}/logs          -> structured executor log
    GET    /api/v1/tasks/{id}/diff          -> real git diff
    POST   /api/v1/tasks/{id}/cancel        -> cancellation
    GET    /api/v1/executors                -> real executor availability

Design commitments:

- **submit returns immediately.** The HTTP request never waits for
  coding to finish; the response is the durable task id and the first
  state.
- **no fake progress.** ``progress`` comes from ``STAGE_PROGRESS`` keyed
  by the stage the task is genuinely in.
- **SSE with exact reconnect.** Every event carries a monotonic
  ``seq``; a client reconnects with ``?after=<seq>`` or
  ``Last-Event-ID`` and replays from the store, so no event is lost
  across a reload, a sleep, or a runtime restart.
- **authorization reuses the product's own surface.** Task reads and
  writes go through the same principal resolution and RBAC matrix the
  rest of the API uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.background.models import TaskRequest, TaskState, TERMINAL_STATES
from app.background.service import get_engine, get_store
from app.api.auth import resolve_principal
from app.workers.safe_subprocess import run as run_command

router = APIRouter(prefix="/api/v1", tags=["tasks"])

TASK_READ_PERMISSIONS = ("tasks.read", "tasks.read.all", "missions.read.all")
TASK_WRITE_PERMISSIONS = ("tasks.create", "missions.create")
TASK_CANCEL_PERMISSIONS = ("tasks.cancel", "tasks.cancel.own", "missions.cancel")


def _principal(authorization: Optional[str]):
    return resolve_principal(authorization)


def _require(principal, permissions: tuple[str, ...], detail: str) -> None:
    if not any(principal.can(permission) for permission in permissions):
        raise HTTPException(status_code=403, detail=detail)


def _load(task_id: str):
    if not _valid_task_id(task_id):
        raise HTTPException(status_code=422, detail="invalid task id")
    task = get_store().get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _valid_task_id(task_id: str) -> bool:
    """Task ids are opaque and never touch the filesystem.

    Validation is still explicit so a crafted id can never be used as
    a path or a SQL wildcard.
    """
    if not task_id or len(task_id) > 64:
        return False
    return all(char.isalnum() or char in {"_", "-"} for char in task_id)


# --------------------------------------------------------------- submit
@router.post("/tasks", status_code=202)
def create_task(
    request: TaskRequest,
    authorization: str | None = Header(default=None),
):
    principal = _principal(authorization)
    _require(principal, TASK_WRITE_PERMISSIONS, "missing permission: tasks.create")

    engine = get_engine()
    try:
        task = engine.submit(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "task_id": task.id,
        "id": task.id,
        "state": task.state.value,
        "planner": task.planner,
        "repo": task.repo,
    }


# ----------------------------------------------------------------- list
@router.get("/tasks")
def list_tasks(
    authorization: str | None = Header(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    state: Optional[str] = None,
):
    principal = _principal(authorization)
    _require(principal, TASK_READ_PERMISSIONS, "missing permission: tasks.read")

    states = None
    if state:
        try:
            states = [TaskState(value.strip().upper()) for value in state.split(",")]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="unknown task state") from exc

    tasks = get_store().list(limit=limit, states=states)
    return {
        "tasks": [task.public_view() for task in tasks],
        "counts": get_store().count_by_state(),
    }


# --------------------------------------------------------------- detail
@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    authorization: str | None = Header(default=None),
):
    principal = _principal(authorization)
    _require(principal, TASK_READ_PERMISSIONS, "missing permission: tasks.read")
    return _load(task_id).public_view()


@router.get("/tasks/{task_id}/logs")
def get_task_logs(
    task_id: str,
    authorization: str | None = Header(default=None),
    limit: int = Query(default=500, ge=1, le=4000),
):
    principal = _principal(authorization)
    _require(principal, TASK_READ_PERMISSIONS, "missing permission: tasks.read")
    _load(task_id)
    return {"task_id": task_id, "logs": get_store().logs(task_id, limit=limit)}


@router.get("/tasks/{task_id}/diff")
def get_task_diff(
    task_id: str,
    authorization: str | None = Header(default=None),
):
    principal = _principal(authorization)
    _require(principal, TASK_READ_PERMISSIONS, "missing permission: tasks.read")
    task = _load(task_id)

    diff = ""
    source = None
    if task.worktree and Path(task.worktree).exists():
        source = "worktree"
        result = run_command(
            ["git", "diff", f"{task.base_sha}..HEAD"] if task.commit_sha else ["git", "diff"],
            cwd=str(task.worktree),
            timeout=120,
        )
        diff = result.get("stdout") or ""
    elif task.commit_sha and task.repo:
        source = "repository"
        result = run_command(
            ["git", "show", "--stat", "--patch", task.commit_sha],
            cwd=str(task.repo),
            timeout=120,
        )
        diff = result.get("stdout") or ""

    if task.verification and task.verification.diff and not diff:
        source = "verification-evidence"
        diff = task.verification.diff

    return {
        "task_id": task_id,
        "source": source,
        "commit": task.commit_sha,
        "branch": task.branch,
        "files_changed": task.files_changed,
        "diff": diff[:200_000],
    }


# --------------------------------------------------------------- cancel
@router.post("/tasks/{task_id}/cancel")
def cancel_task(
    task_id: str,
    authorization: str | None = Header(default=None),
):
    principal = _principal(authorization)
    _require(principal, TASK_CANCEL_PERMISSIONS, "missing permission: tasks.cancel")
    _load(task_id)

    outcome = get_engine().cancel(task_id)
    if outcome == "unknown":
        raise HTTPException(status_code=404, detail="task not found")
    if outcome == "terminal":
        raise HTTPException(status_code=409, detail="task already finished")
    if outcome == "cancelled":
        return {"id": task_id, "state": TaskState.cancelled.value}
    return {
        "id": task_id,
        "state": "CANCELLING",
        "detail": "cancellation requested; the executor stops at the next checkpoint",
    }


# ------------------------------------------------------------ executors
@router.get("/executors")
def list_executors(
    refresh: bool = Query(default=False),
    authorization: str | None = Header(default=None),
):
    """Live executor health (TTL-cached; ``?refresh=true`` re-probes).

    Every entry carries the full health ladder (installed/
    authenticated/model_available/inference_ok), the routing verdict
    (``healthy``/``eligible``), and the real error reason when
    ineligible — the UI shows why an executor is unhealthy.
    """
    principal = _principal(authorization)
    _require(principal, TASK_READ_PERMISSIONS, "missing permission: tasks.read")
    registry = get_engine().registry
    if refresh:
        return registry.refresh()
    return registry.status()


@router.get("/tasks-engine/status")
def engine_status(authorization: str | None = Header(default=None)):
    principal = _principal(authorization)
    _require(principal, TASK_READ_PERMISSIONS, "missing permission: tasks.read")
    return get_engine().status()


# ------------------------------------------------------------------ SSE
@router.get("/tasks/{task_id}/events")
def task_events(
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    after: int = Query(default=0, ge=0),
):
    """Server-sent events for one task.

    Reconnect contract: pass ``after`` (or the standard ``Last-Event-ID``
    header) and the stream resumes from the store at the next ``seq``.
    Heartbeats keep proxies from closing an idle stream.
    """
    principal = _principal(authorization)
    _require(principal, TASK_READ_PERMISSIONS, "missing permission: tasks.read")
    _load(task_id)

    last_event_id = request.headers.get("last-event-id")
    start_seq = after
    if last_event_id and last_event_id.isdigit():
        start_seq = max(start_seq, int(last_event_id))

    bus = get_engine().bus
    store = get_store()

    def frames():
        # Anything already stored (including a finished task's history)
        # is replayed first; the client is then live. Client
        # disconnects cancel this generator (ASGI http.disconnect),
        # which is the supported way to reap dead streams.
        for event in bus.stream(task_id, after_seq=start_seq):
            if event is None:
                task = store.get(task_id)
                if task is not None and task.state in TERMINAL_STATES:
                    yield _frame("task.stream_end", {"state": task.state.value})
                    return
                yield ": keep-alive\n\n"
                continue
            yield _sse(event)
            if event.type in {"task.completed", "task.failed", "task.cancelled", "task.blocked"}:
                yield _frame("task.stream_end", {"state": event.type.split(".")[-1]})
                return

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse(event) -> str:
    payload = json.dumps(
        {
            "seq": event.seq,
            "task_id": event.task_id,
            "type": event.type,
            "ts": event.ts,
            "data": event.data,
        }
    )
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def _frame(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
