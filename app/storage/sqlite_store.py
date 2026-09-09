from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.models import Mission, MissionStatus
from app.storage.db import DB_PATH, connect


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class DuplicateMission(Exception):
    """An identical active mission already exists for this repo."""

    def __init__(self, message: str, mission_id: str | None = None):
        super().__init__(message)
        self.mission_id = mission_id


ACTIVE_STATUSES = (
    "QUEUED",
    "RUNNING",
    "VERIFYING",
    "REPAIRING",
    "RECOVERING",
)

EXECUTING_STATUSES = ("RUNNING", "VERIFYING", "REPAIRING")


def _add_column(db, table, column, decl):
    cols = {
        row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migration_1_runtime_columns(db):
    """
    Durable runtime columns, mission events, and indexes.

    Safe on both fresh databases and existing Stage 7 databases:
    missing columns are added and backfilled from the payload.
    """
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS missions (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        )
        """
    )

    _add_column(db, "missions", "status", "TEXT NOT NULL DEFAULT 'QUEUED'")
    _add_column(db, "missions", "goal", "TEXT NOT NULL DEFAULT ''")
    _add_column(db, "missions", "repo_key", "TEXT")
    _add_column(db, "missions", "claimed_by", "TEXT")
    _add_column(db, "missions", "claimed_at", "TEXT")
    _add_column(db, "missions", "heartbeat_at", "TEXT")
    _add_column(
        db, "missions", "cancel_requested", "INTEGER NOT NULL DEFAULT 0"
    )
    _add_column(db, "missions", "created_at", "TEXT")
    _add_column(db, "missions", "updated_at", "TEXT")

    # Backfill structured columns from stored payloads (no-op rows
    # for fresh inserts going forward, which always set columns).
    db.execute(
        """
        UPDATE missions SET
            status = COALESCE(json_extract(payload, '$.status'), status),
            goal = COALESCE(json_extract(payload, '$.goal'), ''),
            claimed_by = json_extract(payload, '$.claimed_by'),
            claimed_at = json_extract(payload, '$.claimed_at'),
            heartbeat_at = json_extract(payload, '$.heartbeat_at'),
            cancel_requested = CASE
                WHEN json_extract(payload, '$.cancel_requested') = 1
                    THEN 1 ELSE cancel_requested END
        """
    )

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_missions_status_created "
        "ON missions(status, created_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_missions_repo_status "
        "ON missions(repo_key, status)"
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS mission_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 0,
            timestamp TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_mission "
        "ON mission_events(mission_id, id)"
    )


def _migration_2_multi_tenant(db):
    """
    Stage 9 multi-tenancy: client attribution and queue priority
    columns on missions, plus the durable outbox used for
    exactly-once learning-record relay.
    """
    _add_column(db, "missions", "client_id", "TEXT")
    _add_column(db, "missions", "priority", "INTEGER NOT NULL DEFAULT 5")

    # The queue read path: priority first, then submission order.
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_missions_queue_order "
        "ON missions(status, priority, created_at)"
    )

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS mission_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mission_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at TEXT,
            last_error TEXT
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_pending "
        "ON mission_outbox(delivered_at, id)"
    )


MIGRATIONS = [_migration_1_runtime_columns, _migration_2_multi_tenant]


def _migrate(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    version = db.execute("PRAGMA user_version").fetchone()[0]

    for index, fn in enumerate(MIGRATIONS, start=1):
        if index <= version:
            continue

        fn(db)
        db.execute(
            "INSERT OR REPLACE INTO schema_migrations(version, applied_at) "
            "VALUES(?, ?)",
            (index, now_ts()),
        )
        db.execute(f"PRAGMA user_version = {index}")


class MissionStore:
    """
    Durable mission store + queue.

    Stage 8 semantics:
    - missions persist full runtime state (claim, heartbeat,
      cancellation, attempt counters, timestamps)
    - claiming is one immediate transaction, so two concurrent
      coordinators can never receive the same mission
    - heartbeats identify live executors; stale executing missions
      are recoverable after a crash
    - structured mission events persist alongside evidence
    """

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with connect(self.path) as db:
            _migrate(db)

    # -----------------------------------------------------
    # CRUD
    # -----------------------------------------------------

    def save(self, mission: Mission):
        """
        Persist the full mission payload and mirror runtime columns.

        cancel_requested is monotonic: a cancellation requested by
        the API is never un-set by a coordinator save racing it.
        """
        payload = mission.model_dump_json()
        now = now_ts()

        with connect(self.path) as db:
            db.execute(
                """
                INSERT INTO missions(
                    id, payload, status, goal, repo_key,
                    claimed_by, claimed_at, heartbeat_at,
                    cancel_requested, created_at, updated_at,
                    client_id, priority
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload=excluded.payload,
                    status=excluded.status,
                    goal=excluded.goal,
                    repo_key=COALESCE(excluded.repo_key, missions.repo_key),
                    claimed_by=excluded.claimed_by,
                    claimed_at=excluded.claimed_at,
                    heartbeat_at=excluded.heartbeat_at,
                    cancel_requested=CASE
                        WHEN missions.cancel_requested=1 THEN 1
                        ELSE excluded.cancel_requested END,
                    updated_at=excluded.updated_at,
                    client_id=COALESCE(excluded.client_id, missions.client_id),
                    priority=excluded.priority
                """,
                (
                    mission.id,
                    payload,
                    mission.status.value,
                    mission.goal,
                    self._repo_key(mission),
                    mission.claimed_by,
                    mission.claimed_at,
                    mission.heartbeat_at,
                    int(mission.cancel_requested),
                    mission.created_at,
                    now,
                    mission.client_id,
                    mission.priority,
                ),
            )

    def get(self, mission_id: str) -> Mission | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT payload FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()

        if not row:
            return None

        return Mission.model_validate_json(row[0])

    def list(self) -> list[Mission]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT payload FROM missions ORDER BY rowid DESC"
            ).fetchall()

        return [Mission.model_validate_json(row[0]) for row in rows]

    def status_counts(self) -> dict:
        """Runtime observability: missions per status."""
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM missions GROUP BY status"
            ).fetchall()

        return {row[0]: row[1] for row in rows}

    # -----------------------------------------------------
    # Queue operations
    # -----------------------------------------------------

    @staticmethod
    def _repo_key(mission: Mission) -> str:
        repo_path = mission.metadata.get("repo_path")
        if repo_path:
            return str(repo_path)
        return f"capability:{mission.capability}"

    def enqueue(self, mission: Mission) -> None:
        """
        Insert a mission in QUEUED state.

        Single-flight protection: rejects an identical active
        mission (same repo + same goal) so duplicate submissions
        cannot run the same work item twice. Stage 9: carries the
        submitting client's identity and queue priority.
        """
        repo_key = self._repo_key(mission)

        with connect(self.path) as db:
            row = db.execute(
                """
                SELECT id FROM missions
                WHERE repo_key=? AND goal=? AND id != ? AND status IN
                    ('QUEUED','RUNNING','VERIFYING','REPAIRING','RECOVERING')
                """,
                (repo_key, mission.goal, mission.id),
            ).fetchone()

            if row:
                raise DuplicateMission(
                    f"mission {row[0]} already active "
                    "for this repo and goal",
                    mission_id=row[0],
                )

            now = now_ts()

            db.execute(
                """
                INSERT INTO missions(
                    id, payload, status, goal, repo_key,
                    claimed_by, claimed_at, heartbeat_at,
                    cancel_requested, created_at, updated_at,
                    client_id, priority
                )
                VALUES(?, ?, 'QUEUED', ?, ?, NULL, NULL, NULL, 0, ?, ?, ?, ?)
                """,
                (
                    mission.id,
                    mission.model_dump_json(),
                    mission.goal,
                    repo_key,
                    mission.created_at,
                    now,
                    mission.client_id,
                    mission.priority,
                ),
            )

        self.record_event(
            mission.id,
            "mission.queued",
            attempt=0,
            data={"goal": mission.goal, "capability": mission.capability},
        )

    def claim_next(
        self,
        coordinator_id: str,
        skip_repo_keys: set[str] | None = None,
        client_limits: dict[str, int] | None = None,
    ) -> Mission | None:
        """
        Atomically claim the next QUEUED mission.

        Runs inside one immediate transaction: concurrent claimers
        serialize on the write lock, so a mission is handed out at
        most once per claim. skip_repo_keys lets a coordinator that
        already holds repo locks avoid claiming more work for the
        same repositories (per-repo admission control).

        Stage 9 fairness and quotas:
        - candidates are ordered by priority (1 = highest), then
          submission time (FIFO within a priority class)
        - a candidate whose client is already at its concurrency
          limit (counting executing missions only) is skipped in
          the same transaction, so quota enforcement is race-free
        - quota-blocked candidates do not block lower-priority
          work: the scan continues to the next candidate
        """
        skip = {str(k) for k in (skip_repo_keys or set())}
        limits = client_limits or {}

        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")

            placeholders = ",".join("?" for _ in skip)
            query = (
                "SELECT id, payload FROM missions "
                "WHERE status='QUEUED'"
            )
            params: list = []

            if skip:
                query += f" AND repo_key NOT IN ({placeholders})"
                params.extend(sorted(skip))

            query += (
                " ORDER BY priority ASC, created_at ASC, rowid ASC "
                "LIMIT 25"
            )

            rows = db.execute(query, params).fetchall()

            for mission_id, payload in rows:
                mission = Mission.model_validate_json(payload)

                # Per-client concurrency quota, evaluated inside
                # the claim transaction: the quota bounds
                # simultaneous executions, so only executing
                # missions count against it (queued candidates are
                # waiting, not running).
                limit = (
                    limits.get(mission.client_id)
                    if mission.client_id
                    else None
                )

                if limit is not None:
                    others = db.execute(
                        """
                        SELECT COUNT(*) FROM missions
                        WHERE client_id=? AND status IN
                            ('RUNNING','VERIFYING','REPAIRING',
                             'RECOVERING')
                        """,
                        (mission.client_id,),
                    ).fetchone()[0]

                    if others >= limit:
                        continue

                now = now_ts()
                mission.status = MissionStatus.running
                mission.claimed_by = coordinator_id
                mission.claimed_at = now
                mission.heartbeat_at = now
                mission.started_at = mission.started_at or now
                mission.attempt = mission.attempt + 1

                db.execute(
                    """
                    UPDATE missions SET
                        payload=?,
                        status='RUNNING',
                        claimed_by=?,
                        claimed_at=?,
                        heartbeat_at=?,
                        updated_at=?
                    WHERE id=? AND status='QUEUED'
                    """,
                    (
                        mission.model_dump_json(),
                        coordinator_id,
                        now,
                        now,
                        now,
                        mission_id,
                    ),
                )

                if db.total_changes == 0:
                    db.rollback()
                    return None

                db.commit()
                return mission

            db.rollback()
            return None

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def heartbeat(self, mission_id: str, coordinator_id: str) -> bool:
        """
        Refresh the executor heartbeat; False if no longer ours.

        The timestamp is written to BOTH the runtime column and
        the JSON payload in one immediate transaction. Every
        reader of mission state (API polling, watchdog staleness,
        mission listing) consumes the payload, so a heartbeat that
        only touched the column would be invisible to them and a
        live mission could be recovered by mistake.
        """
        now = now_ts()

        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT payload, cancel_requested FROM missions
                WHERE id=? AND claimed_by=? AND status IN
                    ('RUNNING','VERIFYING','REPAIRING')
                """,
                (mission_id, coordinator_id),
            ).fetchone()

            if not row:
                db.rollback()
                return False

            payload = json.loads(row[0])
            payload["heartbeat_at"] = now

            # cancel_requested is monotonic: a cancellation that
            # raced this heartbeat must survive the payload
            # rewrite (single immediate transaction, so no other
            # writer can interleave here).
            if row[1]:
                payload["cancel_requested"] = True

            db.execute(
                """
                UPDATE missions
                SET payload=?, heartbeat_at=?, updated_at=?
                WHERE id=? AND claimed_by=?
                """,
                (json.dumps(payload), now, now, mission_id, coordinator_id),
            )

            db.commit()
            return True

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def request_cancel(self, mission_id: str) -> str:
        """
        Request cancellation.

        Returns one of:
        - "cancelled": mission was QUEUED and is now CANCELLED
        - "requested": executing mission flagged; worker will stop
          at the next cancellation checkpoint
        - "terminal": mission already finished
        - "unknown": no such mission
        """
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                "SELECT status FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()

            if not row:
                db.rollback()
                return "unknown"

            status = row[0]

            if status not in ACTIVE_STATUSES:
                db.rollback()
                return "terminal"

            now = now_ts()
            db.execute(
                """
                UPDATE missions SET
                    cancel_requested=1,
                    payload=json_set(
                        payload, '$.cancel_requested', json('true')
                    ),
                    updated_at=?
                WHERE id=?
                """,
                (now, mission_id),
            )

            if status == "QUEUED":
                db.execute(
                    """
                    UPDATE missions SET
                        status='CANCELLED',
                        payload=json_set(
                            payload,
                            '$.status', 'CANCELLED',
                            '$.finished_at', ?
                        ),
                        updated_at=?
                    WHERE id=?
                    """,
                    (now, now, mission_id),
                )
                db.commit()
                self.record_event(
                    mission_id,
                    "mission.cancelled",
                    attempt=0,
                    data={"while": "QUEUED"},
                )
                return "cancelled"

            db.commit()
            self.record_event(
                mission_id,
                "mission.cancel_requested",
                attempt=0,
                data={"while": status},
            )
            return "requested"

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def stale_executing(self, stale_after_seconds: int) -> list[Mission]:
        """
        Missions stuck in an executing state whose heartbeat is
        older than the cutoff (or missing entirely). Used by the
        watchdog to recover missions after a crash.
        """
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=stale_after_seconds)
        ).isoformat()

        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT payload FROM missions
                WHERE status IN ('RUNNING','VERIFYING','REPAIRING')
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (cutoff,),
            ).fetchall()

        return [Mission.model_validate_json(row[0]) for row in rows]

    # -----------------------------------------------------
    # Structured mission events
    # -----------------------------------------------------

    def record_event(
        self,
        mission_id: str,
        event_type: str,
        attempt: int = 0,
        data: dict | None = None,
    ):
        with connect(self.path) as db:
            cursor = db.execute(
                """
                INSERT INTO mission_events(
                    mission_id, event_type, attempt, timestamp, data
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    mission_id,
                    event_type,
                    attempt,
                    now_ts(),
                    json.dumps(data or {}),
                ),
            )
            return cursor.lastrowid

    def events(self, mission_id: str) -> list[dict]:
        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT mission_id, event_type, attempt, timestamp, data
                FROM mission_events
                WHERE mission_id=?
                ORDER BY id ASC
                """,
                (mission_id,),
            ).fetchall()

        return [
            {
                "mission_id": row[0],
                "event_type": row[1],
                "attempt": row[2],
                "timestamp": row[3],
                "data": json.loads(row[4]),
            }
            for row in rows
        ]

    # -----------------------------------------------------
    # Stage 9.4: per-client concurrency quota
    # -----------------------------------------------------

    def executing_count_for_client(self, client_id: str) -> int:
        """
        Missions currently executing for one client.

        The concurrency quota bounds simultaneous execution, not
        queue depth: queued missions wait their turn and execute
        serially once the limit is reached.
        """
        with connect(self.path) as db:
            row = db.execute(
                """
                SELECT COUNT(*) FROM missions
                WHERE client_id=? AND status IN
                    ('RUNNING','VERIFYING','REPAIRING','RECOVERING')
                """,
                (client_id,),
            ).fetchone()

        return row[0]

    # -----------------------------------------------------
    # Stage 9.5: exactly-once outbox (learning relay)
    # -----------------------------------------------------

    def outbox_enqueue(
        self,
        *,
        mission_id: str,
        kind: str,
        payload: dict,
    ) -> int:
        """Append a message to the durable outbox."""
        with connect(self.path) as db:
            cursor = db.execute(
                """
                INSERT INTO mission_outbox(
                    mission_id, kind, payload, created_at
                )
                VALUES(?, ?, ?, ?)
                """,
                (
                    mission_id,
                    kind,
                    json.dumps(payload),
                    now_ts(),
                ),
            )
            return cursor.lastrowid

    def outbox_pending(self, limit: int = 100) -> list[dict]:
        """Undelivered messages, FIFO, with attempt counts."""
        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT id, mission_id, kind, payload, created_at,
                       attempts
                FROM mission_outbox
                WHERE delivered_at IS NULL
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

        return [
            {
                "id": row[0],
                "mission_id": row[1],
                "kind": row[2],
                "payload": json.loads(row[3]),
                "created_at": row[4],
                "attempts": row[5],
            }
            for row in rows
        ]

    def outbox_mark_delivered(self, outbox_id: int) -> None:
        with connect(self.path) as db:
            db.execute(
                """
                UPDATE mission_outbox
                SET delivered_at=?, attempts=attempts+1
                WHERE id=?
                """,
                (now_ts(), outbox_id),
            )

    def outbox_mark_failed(self, outbox_id: int, error: str) -> None:
        """Record a failed delivery attempt; stays pending for retry."""
        with connect(self.path) as db:
            db.execute(
                """
                UPDATE mission_outbox
                SET attempts=attempts+1, last_error=?
                WHERE id=?
                """,
                (error[:500], outbox_id),
            )

    def outbox_stats(self) -> dict:
        with connect(self.path) as db:
            row = db.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END)
                FROM mission_outbox
                """
            ).fetchone()

        return {
            "total": row[0],
            "pending": row[1] or 0,
            "delivered": row[2] or 0,
        }
