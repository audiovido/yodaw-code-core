"""Durable supervisor repository adapter (SQLite backend).

Owns three tables (jobs, checkpoints, events) in the same database
file as the mission store but with no shared schema. All mutations
run in immediate transactions so concurrent workers serialize on
the write lock.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.storage.db import DB_PATH, connect
from app.supervisor.models import (
    TERMINAL_STATES,
    Checkpoint,
    JobRecord,
    JobState,
    Stage,
    SupervisorEvent,
    now_iso,
)


def _ensure_tables(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS supervisor_jobs (
            job_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'QUEUED',
            mission_id TEXT,
            worker_id TEXT,
            lease_owner TEXT,
            lease_expiry TEXT,
            heartbeat_at TEXT,
            updated_at TEXT,
            next_retry_at TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS supervisor_checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            step INTEGER NOT NULL DEFAULT 0,
            attempt INTEGER NOT NULL DEFAULT 0,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_supervisor_ckpt_job "
        "ON supervisor_checkpoints(job_id, id)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS supervisor_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_supervisor_events_job "
        "ON supervisor_events(job_id, id)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_supervisor_jobs_state "
        "ON supervisor_jobs(state, next_retry_at)"
    )


def _row_to_job(payload: str) -> JobRecord:
    return JobRecord.model_validate_json(payload)


class SupervisorRepository:
    """Persistence adapter behind the Supervisor.

    Kept as an explicit adapter class (rather than free functions)
    so a future backend can implement the same method shape
    without touching supervisor logic.
    """

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.path) as db:
            _ensure_tables(db)

    # ------------------------------------------------- jobs
    def create(self, job: JobRecord) -> JobRecord:
        job.updated_at = now_iso()
        with connect(self.path) as db:
            db.execute(
                """
                INSERT INTO supervisor_jobs(
                    job_id, payload, state, mission_id, worker_id,
                    lease_owner, lease_expiry, heartbeat_at,
                    updated_at, next_retry_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.model_dump_json(),
                    job.state.value,
                    job.mission_id,
                    job.worker_id,
                    job.lease_owner,
                    job.lease_expiry,
                    job.heartbeat_at,
                    job.updated_at,
                    job.next_retry_at,
                ),
            )
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM supervisor_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return _row_to_job(row[0]) if row else None

    def list(self, states: list[JobState] | None = None) -> list[JobRecord]:
        with connect(self.path) as db:
            if states:
                placeholders = ",".join("?" for _ in states)
                rows = db.execute(
                    "SELECT payload FROM supervisor_jobs "
                    f"WHERE state IN ({placeholders}) ORDER BY rowid",
                    [s.value for s in states],
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload FROM supervisor_jobs ORDER BY rowid"
                ).fetchall()
        return [_row_to_job(row[0]) for row in rows]

    def non_terminal(self) -> list[JobRecord]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM supervisor_jobs "
                "WHERE state NOT IN ('FAILED','CANCELLED','PASS') "
                "ORDER BY rowid"
            ).fetchall()
        return [_row_to_job(row[0]) for row in rows]

    def _save(self, db, job: JobRecord) -> None:
        job.updated_at = now_iso()
        db.execute(
            """
            UPDATE supervisor_jobs SET
                payload=?, state=?, mission_id=?, worker_id=?,
                lease_owner=?, lease_expiry=?, heartbeat_at=?,
                updated_at=?, next_retry_at=?
            WHERE job_id=?
            """,
            (
                job.model_dump_json(),
                job.state.value,
                job.mission_id,
                job.worker_id,
                job.lease_owner,
                job.lease_expiry,
                job.heartbeat_at,
                job.updated_at,
                job.next_retry_at,
                job.job_id,
            ),
        )

    def save(self, job: JobRecord) -> None:
        with connect(self.path) as db:
            self._save(db, job)

    # ------------------------------------------------- claim
    def claim(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> JobRecord | None:
        """Atomically claim a QUEUED (or stale-leased) job.

        Returns the claimed job, or None when another live owner
        holds the lease.
        """
        now = datetime.now(timezone.utc)
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        now_s = now.isoformat()
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM supervisor_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return None
            job = _row_to_job(row[0])
            if job.state in TERMINAL_STATES:
                db.rollback()
                return None
            if job.state == JobState.paused and not job.cancel_requested:
                db.rollback()
                return None
            if (
                job.lease_owner
                and job.lease_owner != worker_id
                and job.lease_expiry
                and job.lease_expiry >= now_s
                and job.state in (JobState.claimed, JobState.running)
            ):
                db.rollback()
                return None
            if job.state in (
                JobState.queued,
                JobState.recovering,
                JobState.blocked_external,
            ) or (job.lease_expiry and job.lease_expiry < now_s):
                job.state = JobState.claimed
            job.worker_id = worker_id
            job.lease_owner = worker_id
            job.lease_expiry = expiry
            job.heartbeat_at = now_s
            job.started_at = job.started_at or now_s
            if job.state == JobState.claimed:
                job.attempt += 1
            self._save(db, job)
            db.commit()
            return job
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def claim_next(
        self, worker_id: str, lease_seconds: int
    ) -> JobRecord | None:
        """Claim the oldest claimable job (QUEUED, due retry, stale)."""
        now_s = now_iso()
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """
                SELECT payload FROM supervisor_jobs
                WHERE state NOT IN ('FAILED','CANCELLED','PASS','PAUSED')
                  AND (
                    state = 'QUEUED'
                    OR (state = 'RECOVERING')
                    OR (next_retry_at IS NOT NULL
                        AND next_retry_at <= ?)
                    OR (lease_expiry IS NOT NULL
                        AND lease_expiry < ?
                        AND state IN ('CLAIMED','RUNNING'))
                  )
                ORDER BY rowid ASC LIMIT 10
                """,
                (now_s, now_s),
            ).fetchall()
            for (payload,) in rows:
                job = _row_to_job(payload)
                if (
                    job.lease_owner
                    and job.lease_owner != worker_id
                    and job.lease_expiry
                    and job.lease_expiry >= now_s
                    and job.state in (JobState.claimed, JobState.running)
                ):
                    continue
                expiry = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=lease_seconds)
                ).isoformat()
                if job.state in (
                    JobState.queued,
                    JobState.recovering,
                    JobState.blocked_external,
                ) or (
                    job.next_retry_at and job.next_retry_at <= now_s
                ):
                    job.state = JobState.claimed
                job.worker_id = worker_id
                job.lease_owner = worker_id
                job.lease_expiry = expiry
                job.heartbeat_at = now_s
                job.started_at = job.started_at or now_s
                if job.state == JobState.claimed:
                    job.attempt += 1
                job.next_retry_at = None
                self._save(db, job)
                db.commit()
                return job
            db.rollback()
            return None
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------- heartbeat
    def heartbeat(
        self, job_id: str, worker_id: str, lease_seconds: int
    ) -> bool:
        """Extend the lease; False when the caller lost ownership."""
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM supervisor_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return False
            job = _row_to_job(row[0])
            if job.state in TERMINAL_STATES:
                db.rollback()
                return False
            if job.lease_owner != worker_id:
                db.rollback()
                return False
            now = datetime.now(timezone.utc)
            job.heartbeat_at = now.isoformat()
            job.lease_expiry = (
                now + timedelta(seconds=lease_seconds)
            ).isoformat()
            self._save(db, job)
            db.commit()
            return True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------- checkpoints
    def save_checkpoint(self, checkpoint: Checkpoint) -> int:
        with connect(self.path) as db:
            cursor = db.execute(
                """
                INSERT INTO supervisor_checkpoints(
                    job_id, stage, step, attempt, payload, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.job_id,
                    checkpoint.stage.value,
                    checkpoint.step,
                    checkpoint.attempt,
                    checkpoint.model_dump_json(),
                    checkpoint.created_at,
                ),
            )
            return cursor.lastrowid

    def latest_checkpoint(self, job_id: str) -> Checkpoint | None:
        with connect(self.path) as db:
            row = db.execute(
                """
                SELECT payload FROM supervisor_checkpoints
                WHERE job_id=? ORDER BY id DESC LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return Checkpoint.model_validate_json(row[0]) if row else None

    def checkpoints(self, job_id: str) -> list[Checkpoint]:
        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT payload FROM supervisor_checkpoints
                WHERE job_id=? ORDER BY id ASC
                """,
                (job_id,),
            ).fetchall()
        return [Checkpoint.model_validate_json(row[0]) for row in rows]

    # ------------------------------------------------- events
    def record_event(
        self,
        job_id: str,
        event_type: str,
        attempt: int = 0,
        data: dict[str, Any] | None = None,
    ) -> int:
        with connect(self.path) as db:
            cursor = db.execute(
                """
                INSERT INTO supervisor_events(
                    job_id, event_type, attempt, created_at, data
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    event_type,
                    attempt,
                    now_iso(),
                    json.dumps(data or {}),
                ),
            )
            return cursor.lastrowid

    def events(self, job_id: str) -> list[SupervisorEvent]:
        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT id, job_id, event_type, attempt, created_at, data
                FROM supervisor_events
                WHERE job_id=? ORDER BY id ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            SupervisorEvent(
                seq=row[0],
                job_id=row[1],
                event_type=row[2],
                attempt=row[3],
                created_at=row[4],
                data=json.loads(row[5]),
            )
            for row in rows
        ]
