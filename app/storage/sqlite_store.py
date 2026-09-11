from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.models import Mission, MissionStatus
from app.runtime.repo_identity import repo_identity
from app.storage.db import DB_PATH, connect

# Stage 10.6: shared relay policy. The SQLite store applies the
# same backoff schedule on failed delivery attempts, so the
# backoff behavior is identical for every backend and the relay
# implementation stays transport-agnostic.
OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_BACKOFF_BASE_SECONDS = 1.0
OUTBOX_BACKOFF_MAX_SECONDS = 60.0


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class DuplicateMission(Exception):
    """An identical active mission already exists for this repo."""

    def __init__(self, message: str, mission_id: str | None = None):
        super().__init__(message)
        self.mission_id = mission_id


ACTIVE_STATUSES = (
    "QUEUED",
    "OBSERVING",
    "PLANNING",
    "RUNNING",
    "EXECUTING",
    "VERIFYING",
    "REPAIRING",
    "RECOVERING",
)

EXECUTING_STATUSES = (
    "OBSERVING",
    "PLANNING",
    "RUNNING",
    "EXECUTING",
    "VERIFYING",
    "REPAIRING",
)


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


def _migration_3_stage10_governance(db):
    """
    Stage 10: generalized outbox governance and the audit hash
    chain.

    Outbox: idempotency keys (unique), scheduled retry time,
    and dead-letter state. Existing rows get NULL keys and are
    immediately due, so Stage 9 pending messages relay unchanged.

    Audit: per-event hash-chain columns. Existing Stage 9 rows
    are backfilled into the chain in append (seq) order, so a
    database that never had a chain verifies as intact from its
    inception instead of failing verification.
    """
    _add_column(db, "mission_outbox", "idempotency_key", "TEXT")
    _add_column(db, "mission_outbox", "next_attempt_at", "TEXT")
    _add_column(db, "mission_outbox", "dead_lettered_at", "TEXT")

    # Unique idempotency key; a plain unique index on a column
    # that may hold many historical NULLs is fine in SQLite
    # (NULLs are distinct in unique indexes).
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_idem "
        "ON mission_outbox(idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbox_due "
        "ON mission_outbox(dead_lettered_at, next_attempt_at, id)"
    )

    # The audit table may not exist yet (mission-store migrations
    # run before the audit store is first constructed); create it
    # with the full Stage 10 shape, then backfill the chain.
    from app.tenants.audit import _ensure_table as _ensure_audit_table

    _ensure_audit_table(db)

    # Backfill the chain for pre-Stage-10 rows in append order.
    from app.tenants.audit import canonical_event_payload, event_chain_hash

    rows = db.execute(
        "SELECT seq, ts, actor, client_id, action, mission_id, data "
        "FROM audit_events WHERE event_hash IS NULL ORDER BY seq ASC"
    ).fetchall()

    prev = None
    for seq, ts, actor, client_id, action, mission_id, data in rows:
        digest = event_chain_hash(
            prev,
            canonical_event_payload(
                seq=seq,
                ts=ts,
                actor=actor,
                client_id=client_id,
                action=action,
                mission_id=mission_id,
                data=data,
            ),
        )
        db.execute(
            "UPDATE audit_events SET prev_hash=?, event_hash=? WHERE seq=?",
            (prev, digest, seq),
        )
        prev = digest


def _migration_4_worker_i_product(db):
    """Worker I: product idempotency keys for exactly-once submit."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS mission_idempotency (
            tenant_scope TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (tenant_scope, idempotency_key)
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_idem_mission "
        "ON mission_idempotency(mission_id)"
    )


MIGRATIONS = [
    _migration_1_runtime_columns,
    _migration_2_multi_tenant,
    _migration_3_stage10_governance,
    _migration_4_worker_i_product,
]


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

    # ------------------------------------------------- transactions
    def transact_mission(self, mission_id: str, fn):
        """Apply fn(current) atomically; None from fn means no write.

        The read-modify-write runs in one immediate transaction so
        concurrent writers never lose each other's updates.
        """
        import json

        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if not row:
                db.rollback()
                return None
            mission = Mission.model_validate_json(row[0])
            updated = fn(mission)
            if updated is None:
                db.rollback()
                return None
            payload = updated.model_dump_json()
            now = now_ts()
            db.execute(
                """
                UPDATE missions SET
                    payload=?,
                    status=?,
                    goal=?,
                    repo_key=COALESCE(?, repo_key),
                    claimed_by=?,
                    claimed_at=?,
                    heartbeat_at=?,
                    cancel_requested=CASE
                        WHEN missions.cancel_requested=1 THEN 1
                        ELSE ? END,
                    updated_at=?,
                    client_id=COALESCE(?, client_id),
                    priority=?
                WHERE id=?
                """,
                (
                    payload,
                    updated.status.value,
                    updated.goal,
                    self._repo_key(updated),
                    updated.claimed_by,
                    updated.claimed_at,
                    updated.heartbeat_at,
                    int(updated.cancel_requested),
                    now,
                    updated.client_id,
                    updated.priority,
                    mission_id,
                ),
            )
            db.commit()
            return updated
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def save(self, mission: Mission):
        """
        Persist the full mission payload and mirror runtime columns.

        cancel_requested is monotonic end to end: a cancellation
        requested by the API is never un-set by a coordinator save
        racing it — neither in the runtime column nor inside the
        JSON payload that every reader consumes.
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
                    payload=CASE
                        WHEN missions.cancel_requested=1
                        THEN json_set(
                            excluded.payload,
                            '$.cancel_requested', json('true')
                        )
                        ELSE excluded.payload END,
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
        """
        Canonical single-flight + lease key for a mission target.

        Canonicalization is what makes the key trustworthy: without
        it, `/repo`, `/repo/`, and a symlink to `/repo` would be
        three distinct keys, letting one directory be claimed twice
        concurrently and letting duplicate submissions through.
        """
        return repo_identity(
            mission.metadata.get("repo_path"), mission.capability
        )

    def enqueue(self, mission: Mission) -> None:
        """
        Insert a mission in QUEUED state.

        Single-flight protection: rejects an identical active
        mission (same repo + same goal) so duplicate submissions
        cannot run the same work item twice. The check and the
        insert run in one immediate transaction (mirroring the
        Postgres FOR UPDATE form), so concurrent duplicate
        submissions serialize: exactly one insert wins and the
        losers see the winner's row. Stage 9: carries the
        submitting client's identity and queue priority.
        """
        repo_key = self._repo_key(mission)

        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT id FROM missions
                WHERE repo_key=? AND goal=? AND id != ? AND status IN
                    ('QUEUED','OBSERVING','PLANNING','RUNNING','EXECUTING',
                     'VERIFYING','REPAIRING','RECOVERING')
                """,
                (repo_key, mission.goal, mission.id),
            ).fetchone()

            if row:
                db.rollback()
                raise DuplicateMission(
                    f"mission {row[0]} already active "
                    "for this repo and goal",
                    mission_id=row[0],
                )

            now = now_ts()

            try:
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
            except sqlite3.IntegrityError:
                # Lost a concurrent-insert race against an
                # identical submission: report the winner instead
                # of surfacing a raw constraint violation.
                winner = db.execute(
                    """
                    SELECT id FROM missions
                    WHERE repo_key=? AND goal=? AND id != ?
                      AND status IN
                        ('QUEUED','RUNNING','VERIFYING','REPAIRING',
                         'RECOVERING')
                    """,
                    (repo_key, mission.goal, mission.id),
                ).fetchone()
                db.rollback()
                raise DuplicateMission(
                    f"mission {winner[0] if winner else '?'} already active "
                    "for this repo and goal",
                    mission_id=winner[0] if winner else None,
                )

            db.commit()
        except DuplicateMission:
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

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
                try:
                    mission = Mission.model_validate_json(payload)
                except Exception:
                    # One corrupt payload must not wedge the queue:
                    # leave the row for operator inspection and keep
                    # scanning for healthy candidates.
                    continue

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
                            ('OBSERVING','PLANNING','RUNNING','EXECUTING',
                             'VERIFYING','REPAIRING','RECOVERING')
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
                    ('OBSERVING','PLANNING','RUNNING','EXECUTING',
                     'VERIFYING','REPAIRING','RECOVERING')
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
                WHERE status IN ('OBSERVING','PLANNING','RUNNING',
                                 'EXECUTING','VERIFYING','REPAIRING',
                                 'RECOVERING')
                  AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (cutoff,),
            ).fetchall()

        stale = []
        for row in rows:
            try:
                stale.append(Mission.model_validate_json(row[0]))
            except Exception:
                # Corrupt rows stay for operator inspection; the
                # watchdog keeps recovering the healthy ones.
                continue
        return stale

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
                    ('OBSERVING','PLANNING','RUNNING','EXECUTING',
                     'VERIFYING','REPAIRING','RECOVERING')
                """,
                (client_id,),
            ).fetchone()

        return row[0]

    # -----------------------------------------------------
    # Stage 10.4: persistence-backed token-bucket rate limiting
    # -----------------------------------------------------

    def _ensure_rate_limits_table(self, db) -> None:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                bucket_key TEXT PRIMARY KEY,
                tokens REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )

    def rate_limit_take(
        self,
        bucket_key: str,
        *,
        now: float,
        capacity: int,
        refill_per_second: float,
        tokens: float = 1.0,
    ) -> tuple[bool, float]:
        """
        Atomic token-bucket debit for one client bucket.

        Runs inside one immediate transaction, so multiple API
        processes sharing the database enforce ONE shared bucket
        (multi-process safe by construction). Returns
        (allowed, retry_after_seconds).
        """
        with connect(self.path) as db:
            self._ensure_rate_limits_table(db)
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                "SELECT tokens, updated_at FROM rate_limit_buckets "
                "WHERE bucket_key=?",
                (bucket_key,),
            ).fetchone()

            if row:
                current, updated_at = row
                elapsed = max(0.0, now - updated_at)
                level = min(
                    float(capacity), current + elapsed * refill_per_second
                )
            else:
                # New buckets start full: a first request is always
                # admitted and the burst allowance is available.
                level = float(capacity)

            if level + 1e-9 >= tokens:
                level -= tokens
                allowed = True
                retry_after = 0.0
            else:
                allowed = False
                deficit = tokens - level
                retry_after = (
                    deficit / refill_per_second
                    if refill_per_second > 0
                    else 60.0
                )

            db.execute(
                """
                INSERT INTO rate_limit_buckets(
                    bucket_key, tokens, updated_at
                )
                VALUES(?, ?, ?)
                ON CONFLICT(bucket_key) DO UPDATE SET
                    tokens=excluded.tokens,
                    updated_at=excluded.updated_at
                """,
                (bucket_key, level, now),
            )
            db.commit()

        return allowed, retry_after

    # -----------------------------------------------------
    # Stage 9.5: exactly-once outbox (learning relay)
    # -----------------------------------------------------

    def outbox_enqueue(
        self,
        *,
        mission_id: str,
        kind: str,
        payload: dict,
        idempotency_key: str | None = None,
    ) -> int:
        """
        Append a message to the durable outbox.

        Stage 10.6: an optional idempotency key makes enqueue
        itself replay-safe — producers that crash after commit
        and re-run their transaction insert nothing the second
        time and receive the original message id.
        """
        with connect(self.path) as db:
            if idempotency_key:
                existing = db.execute(
                    """
                    SELECT id FROM mission_outbox
                    WHERE idempotency_key=?
                    """,
                    (idempotency_key,),
                ).fetchone()

                if existing:
                    return existing[0]

            try:
                cursor = db.execute(
                    """
                    INSERT INTO mission_outbox(
                        mission_id, kind, payload, created_at,
                        idempotency_key, next_attempt_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mission_id,
                        kind,
                        json.dumps(payload),
                        now_ts(),
                        idempotency_key,
                        now_ts(),
                    ),
                )
                return cursor.lastrowid
            except sqlite3.IntegrityError:
                # Lost a concurrent idempotent-insert race.
                existing = db.execute(
                    "SELECT id FROM mission_outbox WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return existing[0]
                raise

    def outbox_pending(
        self, limit: int = 100, kinds: list[str] | None = None
    ) -> list[dict]:
        """
        Due undelivered messages, FIFO, with attempt counts.

        Stage 10.6: retry-scheduled messages (backoff after a
        failure) are not due until their next_attempt_at, and
        dead-lettered messages are never pending again until an
        operator requeues them. `kinds` restricts the pass to a
        subset of message kinds (a handler-family drain).
        """
        clauses = [
            "delivered_at IS NULL",
            "dead_lettered_at IS NULL",
            "(next_attempt_at IS NULL OR next_attempt_at <= ?)",
        ]
        params: list = [now_ts()]

        if kinds:
            clauses.append(
                "kind IN (%s)" % ",".join("?" for _ in kinds)
            )
            params.extend(kinds)

        params.append(max(1, int(limit)))

        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT id, mission_id, kind, payload, created_at,
                       attempts
                FROM mission_outbox
                WHERE %s
                ORDER BY id ASC
                LIMIT ?
                """
                % " AND ".join(clauses),
                params,
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
                SET delivered_at=?, attempts=attempts+1,
                    next_attempt_at=NULL
                WHERE id=?
                """,
                (now_ts(), outbox_id),
            )

    def outbox_mark_delivered_if_pending(self, outbox_id: int) -> bool:
        """
        Atomic claim of the delivery acknowledgment.

        Exactly one concurrent relay wins the mark; the loser has
        delivered a replay (at-least-once transport), which
        idempotent handlers absorb. Crash between handler and mark
        leaves the message pending for safe redelivery.
        """
        with connect(self.path) as db:
            cursor = db.execute(
                """
                UPDATE mission_outbox
                SET delivered_at=?, attempts=attempts+1,
                    next_attempt_at=NULL
                WHERE id=? AND delivered_at IS NULL
                  AND dead_lettered_at IS NULL
                """,
                (now_ts(), outbox_id),
            )

            return cursor.rowcount > 0

    def outbox_mark_failed(self, outbox_id: int, error: str) -> None:
        """
        Record a failed delivery attempt.

        Stage 10.6: schedules the retry with exponential backoff
        (shared constants above) and dead-letters the message
        after OUTBOX_MAX_ATTEMPTS so a poison message cannot
        retry forever.
        """
        now = now_ts()

        with connect(self.path) as db:
            row = db.execute(
                "SELECT attempts FROM mission_outbox WHERE id=?",
                (outbox_id,),
            ).fetchone()

            if not row:
                return

            attempts = row[0] + 1

            if attempts >= OUTBOX_MAX_ATTEMPTS:
                db.execute(
                    """
                    UPDATE mission_outbox
                    SET attempts=?, last_error=?, dead_lettered_at=?
                    WHERE id=?
                    """,
                    (attempts, error[:500], now, outbox_id),
                )
                return

            backoff = min(
                OUTBOX_BACKOFF_MAX_SECONDS,
                OUTBOX_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
            )
            next_at = (
                datetime.now(timezone.utc) + timedelta(seconds=backoff)
            ).isoformat()

            db.execute(
                """
                UPDATE mission_outbox
                SET attempts=?, last_error=?, next_attempt_at=?
                WHERE id=?
                """,
                (attempts, error[:500], next_at, outbox_id),
            )

    def outbox_dead_letter(self, outbox_id: int) -> None:
        """Operator action: dead-letter a message immediately."""
        with connect(self.path) as db:
            db.execute(
                """
                UPDATE mission_outbox
                SET dead_lettered_at=?
                WHERE id=? AND delivered_at IS NULL
                """,
                (now_ts(), outbox_id),
            )

    def outbox_list_dead(self, limit: int = 200) -> list[dict]:
        """Dead-lettered messages, oldest first, for inspection."""
        with connect(self.path) as db:
            rows = db.execute(
                """
                SELECT id, mission_id, kind, attempts, last_error,
                       dead_lettered_at
                FROM mission_outbox
                WHERE dead_lettered_at IS NOT NULL
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
                "attempts": row[3],
                "last_error": row[4],
                "dead_lettered_at": row[5],
            }
            for row in rows
        ]

    def outbox_requeue_dead(self, outbox_id: int | None = None) -> int:
        """
        Operator action: return dead-lettered message(s) to the
        pending queue. Returns how many messages were requeued.
        """
        with connect(self.path) as db:
            if outbox_id is None:
                cursor = db.execute(
                    """
                    UPDATE mission_outbox
                    SET dead_lettered_at=NULL, next_attempt_at=?,
                        attempts=0
                    WHERE dead_lettered_at IS NOT NULL
                    """,
                    (now_ts(),),
                )
            else:
                cursor = db.execute(
                    """
                    UPDATE mission_outbox
                    SET dead_lettered_at=NULL, next_attempt_at=?,
                        attempts=0
                    WHERE id=? AND dead_lettered_at IS NOT NULL
                    """,
                    (now_ts(), outbox_id),
                )

            return cursor.rowcount

    def outbox_message(self, outbox_id: int) -> dict | None:
        """One outbox row, for inspection tooling."""
        with connect(self.path) as db:
            row = db.execute(
                """
                SELECT id, mission_id, kind, payload, created_at,
                       attempts, delivered_at, last_error,
                       idempotency_key, next_attempt_at,
                       dead_lettered_at
                FROM mission_outbox WHERE id=?
                """,
                (outbox_id,),
            ).fetchone()

        if not row:
            return None

        return {
            "id": row[0],
            "mission_id": row[1],
            "kind": row[2],
            "payload": json.loads(row[3]),
            "created_at": row[4],
            "attempts": row[5],
            "delivered_at": row[6],
            "last_error": row[7],
            "idempotency_key": row[8],
            "next_attempt_at": row[9],
            "dead_lettered_at": row[10],
        }

    # -----------------------------------------------------
    # Worker I: product idempotency (exactly-once mission submit)
    # -----------------------------------------------------

    def idempotency_lookup(
        self, tenant_scope: str, idempotency_key: str
    ) -> str | None:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT mission_id FROM mission_idempotency "
                "WHERE tenant_scope=? AND idempotency_key=?",
                (tenant_scope, idempotency_key),
            ).fetchone()
        return row[0] if row else None

    def idempotency_claim(
        self, tenant_scope: str, idempotency_key: str, mission_id: str
    ) -> str:
        """Claim one key; the winner's mission id wins every replay."""
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT mission_id FROM mission_idempotency "
                "WHERE tenant_scope=? AND idempotency_key=?",
                (tenant_scope, idempotency_key),
            ).fetchone()
            if row:
                db.rollback()
                return row[0]
            # A bare claim leaves a dangling pointer if the caller
            # dies before inserting the mission row. New code must
            # use submit_idempotent_mission; this path inserts the
            # mapping only when the mission row already exists.
            target = db.execute(
                "SELECT id FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if not target:
                db.rollback()
                return mission_id
            db.execute(
                "INSERT INTO mission_idempotency("
                "tenant_scope, idempotency_key, mission_id, created_at) "
                "VALUES(?, ?, ?, ?)",
                (tenant_scope, idempotency_key, mission_id, now_ts()),
            )
            db.commit()
            return mission_id

    def submit_idempotent_mission(
        self,
        mission: Mission,
        *,
        tenant_scope: str,
        idempotency_key: str | None,
    ) -> tuple[Mission, bool]:
        """Claim idempotency key and insert mission in one tx.

        Returns (mission, replayed). Either both rows exist or
        neither does; concurrent same-key submits yield exactly
        one mission row. Raises DuplicateMission on single-flight
        duplicate (no idempotency row written).
        """
        if not idempotency_key:
            self.enqueue(mission)
            stored = self.get(mission.id)
            return (stored or mission), False
        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT mission_id FROM mission_idempotency "
                "WHERE tenant_scope=? AND idempotency_key=?",
                (tenant_scope, idempotency_key),
            ).fetchone()
            if row:
                winner = db.execute(
                    "SELECT payload FROM missions WHERE id=?",
                    (row[0],),
                ).fetchone()
                db.rollback()
                if not winner:
                    # Crash window row with no mission: treat as
                    # missing so the resubmit claims fresh.
                    return mission, False
                return Mission.model_validate_json(winner[0]), True
            repo_key = self._repo_key(mission)
            dup = db.execute(
                """
                SELECT id FROM missions
                WHERE repo_key=? AND goal=? AND id != ? AND status IN
                    ('QUEUED','OBSERVING','PLANNING','RUNNING','EXECUTING',
                     'VERIFYING','REPAIRING','RECOVERING')
                """,
                (repo_key, mission.goal, mission.id),
            ).fetchone()
            if dup:
                db.rollback()
                raise DuplicateMission(
                    f"mission {dup[0]} already active "
                    "for this repo and goal",
                    mission_id=dup[0],
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
            db.execute(
                "INSERT INTO mission_idempotency("
                "tenant_scope, idempotency_key, mission_id, created_at) "
                "VALUES(?, ?, ?, ?)",
                (tenant_scope, idempotency_key, mission.id, now_ts()),
            )
            db.commit()
        except DuplicateMission:
            raise
        except sqlite3.IntegrityError:
            db.rollback()
            row = self.idempotency_lookup(tenant_scope, idempotency_key)
            if row:
                winner = self.get(row)
                if winner is not None:
                    return winner, True
            # Same-id replay (e.g. dry-run resubmit): mission row
            # already exists, mapping missing -> recreate mapping.
            existing = self.get(mission.id)
            if existing is not None:
                self.idempotency_claim(
                    tenant_scope, idempotency_key, mission.id
                )
                return existing, True
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        stored = self.get(mission.id)
        event_ok = True
        try:
            self.record_event(
                mission.id,
                "mission.queued",
                attempt=0,
                data={"goal": mission.goal, "capability": mission.capability},
            )
        except Exception:
            event_ok = False
        if stored is None:
            raise RuntimeError("mission insert committed but row missing")
        return stored, False

    def outbox_stats(self) -> dict:
        # Pending means deliverable work: undelivered AND not
        # dead-lettered. A dead-lettered message is quarantined for
        # operator review and must never read as queued work.
        with connect(self.path) as db:
            row = db.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN delivered_at IS NULL
                              AND dead_lettered_at IS NULL
                             THEN 1 ELSE 0 END),
                    SUM(CASE WHEN delivered_at IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN dead_lettered_at IS NOT NULL THEN 1 ELSE 0 END)
                FROM mission_outbox
                """
            ).fetchone()

        return {
            "total": row[0],
            "pending": row[1] or 0,
            "delivered": row[2] or 0,
            "dead_lettered": row[3] or 0,
        }
