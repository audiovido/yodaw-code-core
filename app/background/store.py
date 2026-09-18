"""Durable, restart-safe storage for background tasks.

Reuses the project's shared SQLite hardening (``app.storage.db``: WAL,
busy timeout, explicit transactions) and adds the task tables.

Why a dedicated store instead of overloading the mission store:

- the background Task API is a *product* surface with its own
  lifecycle, event vocabulary, and progress semantics; the mission
  store contract stays untouched for the existing coordinator path
- tasks are keyed by their own id space (``t_*``) so a task and a
  mission can never be confused in the UI

Nothing critical is kept in RAM: state, plan, verification evidence,
executor identity, worktree/branch/commit, timestamps, and the full
ordered event log all live in SQLite. A process restart therefore
loses at most the in-flight stage, never the record.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from app.background.models import (
    TERMINAL_STATES,
    PlanResult,
    TaskEvent,
    TaskRecord,
    TaskState,
    VerificationReport,
    now_iso,
)

DEFAULT_TASK_DB = Path(
    os.environ.get("KODGAR_TASKS_DB", "data/kodgar_tasks.db")
)

# Bounded executor output kept per task, so a runaway CLI cannot grow
# the database without limit.
MAX_LOG_LINES = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    repo TEXT,
    project_id TEXT,
    state TEXT NOT NULL,
    progress INTEGER NOT NULL DEFAULT 0,
    current_step TEXT NOT NULL DEFAULT 'queued',
    planner TEXT NOT NULL DEFAULT 'grok',
    planner_backend TEXT,
    executor TEXT,
    executor_reason TEXT,
    plan_json TEXT,
    preferences_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT,
    files_changed INTEGER NOT NULL DEFAULT 0,
    touched_files_json TEXT NOT NULL DEFAULT '[]',
    tests_passed INTEGER NOT NULL DEFAULT 0,
    tests_failed INTEGER NOT NULL DEFAULT 0,
    build_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    verify_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    verify_json TEXT,
    branch TEXT,
    worktree TEXT,
    base_sha TEXT,
    commit_sha TEXT,
    pid INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    timeout_seconds INTEGER,
    error_json TEXT,
    result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    ts TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_task_events_task
    ON task_events(task_id, seq);

CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    stream TEXT NOT NULL,
    line TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id, id);
"""


def task_row_to_record(row: Any) -> TaskRecord:
    keys = row.keys() if hasattr(row, "keys") else []
    data = {key: row[key] for key in keys}
    return TaskRecord(
        id=data["id"],
        goal=data["goal"],
        repo=data.get("repo"),
        project_id=data.get("project_id"),
        state=TaskState(data["state"]),
        progress=int(data.get("progress") or 0),
        current_step=data.get("current_step") or "queued",
        planner=data.get("planner") or "grok",
        planner_backend=data.get("planner_backend"),
        executor=data.get("executor"),
        executor_reason=data.get("executor_reason"),
        plan=(
            PlanResult.model_validate_json(data["plan_json"])
            if data.get("plan_json")
            else None
        ),
        preferences=json.loads(data.get("preferences_json") or "{}"),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        started_at=data.get("started_at"),
        finished_at=data.get("finished_at"),
        heartbeat_at=data.get("heartbeat_at"),
        files_changed=int(data.get("files_changed") or 0),
        touched_files=json.loads(data.get("touched_files_json") or "[]"),
        tests_passed=int(data.get("tests_passed") or 0),
        tests_failed=int(data.get("tests_failed") or 0),
        build_status=data.get("build_status") or "UNKNOWN",
        verify_status=data.get("verify_status") or "UNKNOWN",
        verification=(
            VerificationReport.model_validate_json(data["verify_json"])
            if data.get("verify_json")
            else None
        ),
        branch=data.get("branch"),
        worktree=data.get("worktree"),
        base_sha=data.get("base_sha"),
        commit_sha=data.get("commit_sha"),
        pid=data.get("pid"),
        cancel_requested=bool(data.get("cancel_requested")),
        timeout_seconds=data.get("timeout_seconds"),
        error=json.loads(data["error_json"]) if data.get("error_json") else None,
        result=json.loads(data.get("result_json") or "{}"),
    )


class TaskStore:
    """SQLite-backed task persistence.

    Every mutation is one transaction. Event sequence numbers come from
    SQLite's AUTOINCREMENT, so ordering is global and monotonic, and an
    SSE client can resume from any ``seq`` it already saw.
    """

    def __init__(self, db_path=DEFAULT_TASK_DB):
        self.db_path = Path(db_path)
        # One connection, serialized by one lock.
        #
        # Two designs were tried and one was rejected on evidence: a
        # per-thread connection pool deadlocks when a thread lazily
        # opens a connection (which runs `PRAGMA journal_mode=WAL`, an
        # exclusive operation) while another thread is committing — the
        # writer waits for the pragma holder and the pragma waits for
        # the writer. A single connection shared across the API thread,
        # the task worker threads, and the SSE stream, with every
        # statement inside `self._lock`, removes the class of problem
        # entirely. Task traffic is low-volume and every statement here
        # is short, so serialization costs nothing measurable.
        self._lock = threading.RLock()
        self.db = self._open()
        with self._lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _open(self):
        """Hardened connection, safe to share across threads.

        Mirrors ``app.storage.db.connect`` (WAL, busy timeout, explicit
        transaction boundaries) with one difference: this store is
        driven from multiple threads, so the connection is created with
        ``check_same_thread=False`` and every access is serialized by
        the store's own lock.
        """
        import sqlite3 as _sqlite3

        from app.storage.db import BUSY_TIMEOUT_MS

        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _sqlite3.OperationalError(
                f"cannot create task database directory "
                f"{self.db_path.parent}: {exc}. Set KODGAR_TASKS_DB to a "
                f"writable location."
            ) from exc

        try:
            db = _sqlite3.connect(
                self.db_path,
                timeout=BUSY_TIMEOUT_MS / 1000,
                check_same_thread=False,
            )
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            db.execute("PRAGMA synchronous=NORMAL")
        except _sqlite3.OperationalError as exc:
            raise _sqlite3.OperationalError(
                f"cannot open task database {self.db_path}: {exc}. "
                f"Set KODGAR_TASKS_DB to a writable location."
            ) from exc

        db.row_factory = _sqlite3.Row
        return db

    # ---------------------------------------------------------- create
    def create(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            self.db.execute(
                """
                INSERT INTO tasks (
                    id, goal, repo, project_id, state, progress,
                    current_step, planner, planner_backend, executor,
                    executor_reason, plan_json, preferences_json,
                    created_at, updated_at, started_at, finished_at,
                    heartbeat_at, files_changed, touched_files_json,
                    tests_passed, tests_failed, build_status,
                    verify_status, verify_json, branch, worktree,
                    base_sha, commit_sha, pid, cancel_requested,
                    timeout_seconds, error_json, result_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    task.id,
                    task.goal,
                    task.repo,
                    task.project_id,
                    task.state.value,
                    task.progress,
                    task.current_step,
                    task.planner,
                    task.planner_backend,
                    task.executor,
                    task.executor_reason,
                    _dump(task.plan),
                    json.dumps(task.preferences),
                    task.created_at,
                    task.updated_at,
                    task.started_at,
                    task.finished_at,
                    task.heartbeat_at,
                    task.files_changed,
                    json.dumps(task.touched_files),
                    task.tests_passed,
                    task.tests_failed,
                    task.build_status,
                    task.verify_status,
                    _dump(task.verification),
                    task.branch,
                    task.worktree,
                    task.base_sha,
                    task.commit_sha,
                    task.pid,
                    1 if task.cancel_requested else 0,
                    task.timeout_seconds,
                    json.dumps(task.error) if task.error else None,
                    json.dumps(task.result),
                ),
            )
            self.db.commit()
        return task

    # ------------------------------------------------------------ read
    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return task_row_to_record(row) if row is not None else None

    def list(
        self,
        limit: int = 100,
        states: Optional[list[TaskState]] = None,
        project_id: Optional[str] = None,
    ) -> list[TaskRecord]:
        sql = "SELECT * FROM tasks"
        clauses: list[str] = []
        params: list[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(state.value for state in states)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._lock:
            rows = self.db.execute(sql, tuple(params)).fetchall()
        return [task_row_to_record(row) for row in rows]

    def count_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self.db.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
            ).fetchall()
        return {row["state"]: row["n"] for row in rows}

    # ---------------------------------------------------------- mutate
    def update(self, task_id: str, **fields: Any) -> Optional[TaskRecord]:
        """Patch whitelisted columns; serializes structured values."""
        allowed = {
            "state",
            "progress",
            "current_step",
            "executor",
            "executor_reason",
            "planner",
            "planner_backend",
            "plan",
            "preferences",
            "started_at",
            "finished_at",
            "heartbeat_at",
            "updated_at",
            "files_changed",
            "touched_files",
            "tests_passed",
            "tests_failed",
            "build_status",
            "verify_status",
            "verification",
            "branch",
            "worktree",
            "base_sha",
            "commit_sha",
            "pid",
            "cancel_requested",
            "timeout_seconds",
            "error",
            "result",
            "repo",
            "project_id",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unwritable task field: {key}")
            column, encoded = _encode(key, value)
            assignments.append(f"{column} = ?")
            params.append(encoded)
        if not assignments:
            return self.get(task_id)
        assignments.append("updated_at = ?")
        params.append(now_iso())
        params.append(task_id)
        with self._lock:
            cursor = self.db.execute(
                f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
                tuple(params),
            )
            self.db.commit()
        if cursor.rowcount == 0:
            return None
        return self.get(task_id)

    def transition(
        self,
        task_id: str,
        state: TaskState,
        step: str,
        progress: Optional[int] = None,
        **fields: Any,
    ) -> Optional[TaskRecord]:
        """State change + progress + heartbeat in one transaction."""
        from app.background.models import STAGE_PROGRESS

        payload = dict(fields)
        payload["state"] = state
        payload["current_step"] = step
        payload["progress"] = (
            STAGE_PROGRESS[state] if progress is None else progress
        )
        payload["heartbeat_at"] = now_iso()
        if state not in TERMINAL_STATES and not payload.get("started_at"):
            current = self.get(task_id)
            if current is not None and current.started_at is None:
                payload["started_at"] = now_iso()
        if state in TERMINAL_STATES:
            payload["finished_at"] = now_iso()
        return self.update(task_id, **payload)

    # ---------------------------------------------------------- events
    def append_event(
        self, task_id: str, event_type: str, data: Optional[dict] = None
    ) -> TaskEvent:
        ts = now_iso()
        payload = json.dumps(data or {})
        with self._lock:
            cursor = self.db.execute(
                """
                INSERT INTO task_events (task_id, type, ts, data_json)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, event_type, ts, payload),
            )
            self.db.commit()
            seq = int(cursor.lastrowid)
        return TaskEvent(
            seq=seq, task_id=task_id, type=event_type, ts=ts, data=data or {}
        )

    def events(
        self, task_id: str, after_seq: int = 0, limit: int = 1000
    ) -> list[TaskEvent]:
        with self._lock:
            rows = self.db.execute(
                """
                SELECT seq, task_id, type, ts, data_json FROM task_events
                WHERE task_id = ? AND seq > ?
                ORDER BY seq ASC LIMIT ?
                """,
                (task_id, int(after_seq), int(limit)),
            ).fetchall()
        return [
            TaskEvent(
                seq=row["seq"],
                task_id=row["task_id"],
                type=row["type"],
                ts=row["ts"],
                data=json.loads(row["data_json"] or "{}"),
            )
            for row in rows
        ]

    def max_seq(self, task_id: str) -> int:
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM task_events "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["n"] or 0)

    # ------------------------------------------------------------ logs
    def append_log(
        self, task_id: str, lines: list[str], stream: str = "stdout"
    ) -> int:
        if not lines:
            return 0
        ts = now_iso()
        with self._lock:
            self.db.executemany(
                "INSERT INTO task_logs (task_id, ts, stream, line) "
                "VALUES (?, ?, ?, ?)",
                [(task_id, ts, stream, line[:4000]) for line in lines],
            )
            self.db.execute(
                """
                DELETE FROM task_logs WHERE task_id = ? AND id NOT IN (
                    SELECT id FROM task_logs WHERE task_id = ?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (task_id, task_id, MAX_LOG_LINES),
            )
            self.db.commit()
            row = self.db.execute(
                "SELECT COUNT(*) AS n FROM task_logs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["n"] or 0)

    def logs(self, task_id: str, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                """
                SELECT ts, stream, line FROM task_logs WHERE task_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (task_id, int(limit)),
            ).fetchall()
        return [
            {"ts": row["ts"], "stream": row["stream"], "line": row["line"]}
            for row in reversed(rows)
        ]

    # -------------------------------------------------- cancellation
    def request_cancel(self, task_id: str) -> str:
        """One of ``cancelled`` | ``requested`` | ``terminal`` | ``unknown``."""
        task = self.get(task_id)
        if task is None:
            return "unknown"
        if task.state in TERMINAL_STATES:
            return "terminal"
        if task.state == TaskState.queued:
            with self._lock:
                self.db.execute(
                    "UPDATE tasks SET cancel_requested = 1, state = ?, "
                    "current_step = 'cancelled', finished_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (TaskState.cancelled.value, now_iso(), now_iso(), task_id),
                )
                self.db.commit()
            return "cancelled"
        self.update(task_id, cancel_requested=True)
        return "requested"

    def cancel_requested(self, task_id: str) -> bool:
        with self._lock:
            row = self.db.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    # ------------------------------------------------ crash recovery
    def recover_interrupted(self) -> list[str]:
        """Requeue every non-terminal task; called once at startup.

        A task whose process died mid-stage is durable but incomplete.
        Requeueing is safe because each attempt allocates a fresh
        isolated worktree, so no partially-written worktree is
        resurrected. The requeue itself is recorded as an event by the
        engine, never silently.
        """
        with self._lock:
            rows = self.db.execute(
                "SELECT id, state FROM tasks WHERE state NOT IN "
                f"({','.join('?' for _ in TERMINAL_STATES)})",
                tuple(state.value for state in TERMINAL_STATES),
            ).fetchall()
        recovered: list[str] = []
        for row in rows:
            with self._lock:
                self.db.execute(
                    "UPDATE tasks SET state = ?, current_step = ?, pid = NULL, "
                    "cancel_requested = 0, updated_at = ? WHERE id = ?",
                    (
                        TaskState.queued.value,
                        f"requeued after restart (was {row['state']})",
                        now_iso(),
                        row["id"],
                    ),
                )
                self.db.commit()
            recovered.append(row["id"])
        return recovered

    def delete(self, task_id: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM task_logs WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self.db.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self.db.close()
            except Exception:  # pragma: no cover - best effort
                pass


def _dump(model: Any) -> Optional[str]:
    if model is None:
        return None
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    return json.dumps(model)


def _encode(key: str, value: Any) -> tuple[str, Any]:
    if key == "state":
        return "state", value.value if hasattr(value, "value") else str(value)
    if key == "plan":
        return "plan_json", _dump(value)
    if key == "verification":
        return "verify_json", _dump(value)
    if key in {"preferences", "error", "result"}:
        return (f"{key}_json", json.dumps(value) if value is not None else None)
    if key == "touched_files":
        return "touched_files_json", json.dumps(value or [])
    if key == "cancel_requested":
        return "cancel_requested", 1 if value else 0
    return key, value
