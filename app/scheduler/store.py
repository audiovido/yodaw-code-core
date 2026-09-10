"""Durable scheduler store (SQLite backend).

Owns four tables (tasks, workers, events, merges) with no shared
schema. Mutations that decide ownership run in immediate
transactions so concurrent schedulers serialize on the write
lock, mirroring the supervisor repository lease semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.scheduler.models import (
    MergeCandidate,
    MergeStatus,
    SchedulingEvent,
    TaskSpec,
    TaskState,
    WorkerSpec,
    now_iso,
)
from app.storage.db import DB_PATH, connect


def _ensure_tables(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_tasks (
            task_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'QUEUED',
            priority INTEGER NOT NULL DEFAULT 5,
            created_seq INTEGER NOT NULL DEFAULT 0,
            capability TEXT NOT NULL DEFAULT 'code',
            lease_owner TEXT,
            lease_expiry TEXT,
            next_retry_at TEXT,
            updated_at TEXT
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sched_tasks_state "
        "ON scheduler_tasks(state, priority, created_seq)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_workers (
            worker_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_sched_events_task "
        "ON scheduler_events(task_id, id)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_merges (
            candidate_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'PENDING',
            payload TEXT NOT NULL
        )
        """
    )


def _row_to_task(payload: str) -> TaskSpec:
    return TaskSpec.model_validate_json(payload)


class SchedulerStore:
    """Persistence adapter behind the Scheduler."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.path) as db:
            _ensure_tables(db)

    # ------------------------------------------------- tasks
    def create_tasks(self, tasks: list[TaskSpec]) -> None:
        now = now_iso()
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(MAX(created_seq), 0) FROM scheduler_tasks"
            ).fetchone()
            seq = int(row[0]) if row else 0
            for task in tasks:
                seq += 1
                task.created_seq = seq
                task.created_at = task.created_at or now
                db.execute(
                    """
                    INSERT INTO scheduler_tasks(
                        task_id, payload, state, priority,
                        created_seq, capability, lease_owner,
                        lease_expiry, next_retry_at, updated_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.model_dump_json(),
                        task.state.value,
                        task.priority,
                        task.created_seq,
                        task.capability,
                        task.lease_owner,
                        task.lease_expiry,
                        task.next_retry_at,
                        now,
                    ),
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, task_id: str) -> TaskSpec | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM scheduler_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return _row_to_task(row[0]) if row else None

    def exists(self, task_id: str) -> bool:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT 1 FROM scheduler_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return row is not None

    def list_all(self) -> list[TaskSpec]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM scheduler_tasks ORDER BY created_seq ASC"
            ).fetchall()
        return [_row_to_task(row[0]) for row in rows]

    def list_by_state(self, state: TaskState) -> list[TaskSpec]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM scheduler_tasks WHERE state=? "
                "ORDER BY created_seq ASC",
                (state.value,),
            ).fetchall()
        return [_row_to_task(row[0]) for row in rows]

    def load(self, worker_id: str) -> int:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM scheduler_tasks "
                "WHERE lease_owner=? AND state IN ('ASSIGNED','RUNNING')",
                (worker_id,),
            ).fetchone()
        return int(row[0])

    def global_load(self) -> int:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM scheduler_tasks "
                "WHERE state IN ('ASSIGNED','RUNNING')"
            ).fetchone()
        return int(row[0])

    def _save(self, db, task: TaskSpec) -> None:
        db.execute(
            """
            UPDATE scheduler_tasks SET
                payload=?, state=?, priority=?, created_seq=?,
                capability=?, lease_owner=?, lease_expiry=?,
                next_retry_at=?, updated_at=?
            WHERE task_id=?
            """,
            (
                task.model_dump_json(),
                task.state.value,
                task.priority,
                task.created_seq,
                task.capability,
                task.lease_owner,
                task.lease_expiry,
                task.next_retry_at,
                now_iso(),
                task.task_id,
            ),
        )

    def save(self, task: TaskSpec) -> None:
        with connect(self.path) as db:
            self._save(db, task)

    # ------------------------------------------------- transactions
    def transact_task(self, task_id: str, fn):
        """Apply fn(current) atomically; None from fn means no write.

        The read-modify-write runs in one immediate transaction so
        concurrent schedulers never lose each other's claims to a
        stale blind save. Exceptions roll back and propagate.
        """
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM scheduler_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return None
            updated = fn(_row_to_task(row[0]))
            if updated is None:
                db.rollback()
                return None
            self._save(db, updated)
            db.commit()
            return updated
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------- atomic age
    def age_tasks(self, task_ids: list[str]) -> None:
        """Atomically bump wait_rounds for still-eligible tasks.

        Each task is re-read inside one immediate transaction and
        only incremented when it is still READY or RECOVERING. A
        concurrent claim that moved the task to ASSIGNED/RUNNING
        is never overwritten by a stale blind save.
        """
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            for task_id in task_ids:
                row = db.execute(
                    "SELECT payload FROM scheduler_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if not row:
                    continue
                task = _row_to_task(row[0])
                if task.state not in (
                    TaskState.ready,
                    TaskState.recovering,
                ):
                    continue
                task.wait_rounds += 1
                self._save(db, task)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _assigned_count(self, db, worker_id: str | None = None) -> int:
        if worker_id is None:
            row = db.execute(
                "SELECT COUNT(*) FROM scheduler_tasks "
                "WHERE state IN ('ASSIGNED','RUNNING')"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT COUNT(*) FROM scheduler_tasks "
                "WHERE lease_owner=? AND state IN ('ASSIGNED','RUNNING')",
                (worker_id,),
            ).fetchone()
        return int(row[0])

    # ------------------------------------------------- atomic claim
    def claim_task(
        self,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> TaskSpec | None:
        """Atomically claim one task for a worker.

        Returns the claimed task, or None when the task is not
        claimable or another live owner holds the lease.
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        now_s = now.isoformat()
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM scheduler_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return None
            task = _row_to_task(row[0])
            if task.state in (
                TaskState.completed,
                TaskState.failed,
                TaskState.cancelled,
            ):
                db.rollback()
                return None
            if (
                task.lease_owner
                and task.lease_owner != worker_id
                and task.lease_expiry
                and task.lease_expiry >= now_s
                and task.state in (TaskState.assigned, TaskState.running)
            ):
                db.rollback()
                return None
            if task.state not in (
                TaskState.ready,
                TaskState.recovering,
                TaskState.assigned,
                TaskState.running,
            ):
                db.rollback()
                return None
            task.state = TaskState.assigned
            task.wait_rounds = 0
            task.next_retry_at = None
            task.lease_owner = worker_id
            task.lease_expiry = expiry
            task.heartbeat_at = now_s
            self._save(db, task)
            db.commit()
            return task
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def heartbeat_task(
        self, task_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        from datetime import datetime, timedelta, timezone

        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM scheduler_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return False
            task = _row_to_task(row[0])
            if task.state in (
                TaskState.completed,
                TaskState.failed,
                TaskState.cancelled,
            ):
                db.rollback()
                return False
            if task.lease_owner != worker_id:
                db.rollback()
                return False
            now = datetime.now(timezone.utc)
            task.heartbeat_at = now.isoformat()
            task.lease_expiry = (
                now + timedelta(seconds=lease_seconds)
            ).isoformat()
            self._save(db, task)
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def force_expire(self, task_id: str) -> None:
        """Expire a task lease (recovery testing and ops repair)."""
        task = self.get(task_id)
        if task is None:
            return
        task.lease_expiry = "1970-01-01T00:00:00+00:00"
        self.save(task)

    # ------------------------------------------------- workers
    def upsert_worker(self, worker: WorkerSpec) -> None:
        worker.heartbeat_at = now_iso()
        with connect(self.path) as db:
            db.execute(
                """
                INSERT INTO scheduler_workers(
                    worker_id, payload, updated_at
                )
                VALUES(?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (worker.worker_id, worker.model_dump_json(), now_iso()),
            )

    def get_worker(self, worker_id: str) -> WorkerSpec | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM scheduler_workers WHERE worker_id=?",
                (worker_id,),
            ).fetchone()
        return WorkerSpec.model_validate_json(row[0]) if row else None

    def list_workers(self) -> list[WorkerSpec]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM scheduler_workers ORDER BY worker_id ASC"
            ).fetchall()
        return [WorkerSpec.model_validate_json(row[0]) for row in rows]

    def heartbeat_worker(self, worker_id: str) -> bool:
        worker = self.get_worker(worker_id)
        if worker is None:
            return False
        worker.heartbeat_at = now_iso()
        self.upsert_worker(worker)
        return True

    # ------------------------------------------------- events
    def record_event(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> int:
        with connect(self.path) as db:
            cursor = db.execute(
                """
                INSERT INTO scheduler_events(
                    task_id, event_type, created_at, data
                )
                VALUES(?, ?, ?, ?)
                """,
                (task_id, event_type, now_iso(), json.dumps(data or {})),
            )
            return cursor.lastrowid

    def events(self, task_id: str) -> list[SchedulingEvent]:
        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT id, task_id, event_type, created_at, data
                FROM scheduler_events
                WHERE task_id=? ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            SchedulingEvent(
                seq=row[0],
                task_id=row[1],
                event_type=row[2],
                created_at=row[3],
                data=json.loads(row[4]),
            )
            for row in rows
        ]

    # ------------------------------------------------- merges
    def create_candidate(self, candidate: MergeCandidate) -> None:
        with connect(self.path) as db:
            db.execute(
                """
                INSERT INTO scheduler_merges(
                    candidate_id, task_id, status, payload
                )
                VALUES(?, ?, ?, ?)
                """,
                (
                    candidate.candidate_id,
                    candidate.task_id,
                    candidate.status.value,
                    candidate.model_dump_json(),
                ),
            )

    def save_candidate(self, candidate: MergeCandidate) -> None:
        with connect(self.path) as db:
            db.execute(
                """
                UPDATE scheduler_merges SET
                    task_id=?, status=?, payload=?
                WHERE candidate_id=?
                """,
                (
                    candidate.task_id,
                    candidate.status.value,
                    candidate.model_dump_json(),
                    candidate.candidate_id,
                ),
            )

    def get_candidate(self, candidate_id: str) -> MergeCandidate | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM scheduler_merges WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        return MergeCandidate.model_validate_json(row[0]) if row else None

    def candidate_for_patch(self, task_id: str, patch_id: str) -> MergeCandidate | None:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM scheduler_merges WHERE task_id=?",
                (task_id,),
            ).fetchall()
        for (payload,) in rows:
            candidate = MergeCandidate.model_validate_json(payload)
            if candidate.patch_id == patch_id:
                return candidate
        return None

    def active_candidates(self) -> list[MergeCandidate]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM scheduler_merges WHERE status IN "
                "('PENDING','READY','CONFLICT','NEEDS_REVIEW')"
            ).fetchall()
        return [MergeCandidate.model_validate_json(row[0]) for row in rows]

    def all_candidates(self) -> list[MergeCandidate]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM scheduler_merges ORDER BY rowid ASC"
            ).fetchall()
        return [MergeCandidate.model_validate_json(row[0]) for row in rows]
