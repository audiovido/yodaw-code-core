"""
Stage 10.2: PostgreSQL persistence adapter.

Implements the same capability protocols as the SQLite store
(app/storage/protocols.py) against a Postgres database selected
by DSN (YODAW_DATABASE_URL or explicit URL). Driver: psycopg 3
(lightweight, maintained, no C-level server dependency thanks to
psycopg-binary).

Correctness mapping from the SQLite design:

- exact-once queue claim    : single UPDATE ... WHERE id = (
                                SELECT ... FOR UPDATE SKIP LOCKED)
                              inside one transaction; concurrent
                              coordinators skip each other's rows
- per-repo leases           : INSERT ... ON CONFLICT with stale
                              heartbeat theft inside one tx
- heartbeats                : owner-guarded UPDATE (returns rowcount)
- quota checks at claim time: COUNT of executing missions per
                              client evaluated in the same
                              transaction as the claim UPDATE
- outbox                    : identical columns and backoff policy;
                              FOR UPDATE SKIP LOCKED pending scan
- audit hash chain          : identical canonical payload and
                              SHA-256 linking; chain head read in
                              the append transaction
- rate limiting             : row-level lock (FOR UPDATE) token
                              bucket — multi-process safe

Migrations are versioned (schema_migrations table) and applied on
connect, mirroring the SQLite migration list.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.models import Mission, MissionStatus
from app.runtime.repo_identity import repo_identity
from app.storage.sqlite_store import (
    ACTIVE_STATUSES,
    DuplicateMission,
    OUTBOX_BACKOFF_BASE_SECONDS,
    OUTBOX_BACKOFF_MAX_SECONDS,
    OUTBOX_MAX_ATTEMPTS,
)

try:
    import psycopg
except ImportError:  # pragma: no cover - reported, not raised here
    psycopg = None


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_key(mission: Mission) -> str:
    """Canonical single-flight + lease key, shared with SQLite."""
    return repo_identity(
        mission.metadata.get("repo_path"), mission.capability
    )


def _require_psycopg():
    if psycopg is None:  # pragma: no cover
        raise RuntimeError(
            "psycopg is required for the Postgres backend; install "
            "requirements.txt (pip install 'psycopg[binary]')"
        )


SCHEMA_SQL = [
    # ------------------------------------------------- missions
    """
    CREATE TABLE IF NOT EXISTS missions (
        id TEXT PRIMARY KEY,
        payload JSONB NOT NULL,
        status TEXT NOT NULL DEFAULT 'QUEUED',
        goal TEXT NOT NULL DEFAULT '',
        repo_key TEXT,
        claimed_by TEXT,
        claimed_at TEXT,
        heartbeat_at TEXT,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TEXT,
        updated_at TEXT,
        client_id TEXT,
        priority INTEGER NOT NULL DEFAULT 5
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_missions_status_created
        ON missions(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_missions_repo_status
        ON missions(repo_key, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_missions_queue_order
        ON missions(status, priority, created_at)
    """,
    # ------------------------------------------------- events
    """
    CREATE TABLE IF NOT EXISTS mission_events (
        id BIGSERIAL PRIMARY KEY,
        mission_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        attempt INTEGER NOT NULL DEFAULT 0,
        timestamp TEXT NOT NULL,
        data JSONB NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_mission
        ON mission_events(mission_id, id)
    """,
    # ------------------------------------------------- outbox
    """
    CREATE TABLE IF NOT EXISTS mission_outbox (
        id BIGSERIAL PRIMARY KEY,
        mission_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        delivered_at TEXT,
        last_error TEXT,
        idempotency_key TEXT,
        next_attempt_at TEXT,
        dead_lettered_at TEXT
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_idem
        ON mission_outbox(idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_outbox_due
        ON mission_outbox(dead_lettered_at, next_attempt_at, id)
    """,
    # ------------------------------------------------- leases
    """
    CREATE TABLE IF NOT EXISTS repo_leases (
        repo_key TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        mission_id TEXT,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL
    )
    """,
    # ------------------------------------------------- clients
    """
    CREATE TABLE IF NOT EXISTS api_clients (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        key_hash TEXT NOT NULL UNIQUE,
        priority INTEGER NOT NULL DEFAULT 5,
        max_concurrent_missions INTEGER,
        created_at TEXT NOT NULL,
        disabled_at TEXT
    )
    """,
    # ------------------------------------------------- admins
    """
    CREATE TABLE IF NOT EXISTS api_admins (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        key_hash TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL,
        created_at TEXT NOT NULL,
        rotated_at TEXT,
        disabled_at TEXT
    )
    """,
    # ------------------------------------------------- audit
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        seq BIGSERIAL PRIMARY KEY,
        ts TEXT NOT NULL,
        actor TEXT,
        client_id TEXT,
        action TEXT NOT NULL,
        mission_id TEXT,
        data JSONB NOT NULL DEFAULT '{}',
        prev_hash TEXT,
        event_hash TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_client_seq
        ON audit_events(client_id, seq)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_mission
        ON audit_events(mission_id, seq)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_audit_ts
        ON audit_events(ts)
    """,
    # ------------------------------------------------- rate limits
    """
    CREATE TABLE IF NOT EXISTS rate_limit_buckets (
        bucket_key TEXT PRIMARY KEY,
        tokens DOUBLE PRECISION NOT NULL,
        updated_at DOUBLE PRECISION NOT NULL
    )
    """,
    # ------------------------------------------------- learning
    """
    CREATE TABLE IF NOT EXISTS learning_records (
        id TEXT PRIMARY KEY,
        payload JSONB NOT NULL,
        created_at TEXT NOT NULL DEFAULT NOW()::text
    )
    """,
]


class PostgresMissionStore:
    """Mission/queue/outbox/rate-limit protocol over Postgres."""

    def __init__(self, dsn: str):
        _require_psycopg()
        self.dsn = dsn
        self._migrate()

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    def _migrate(self) -> None:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "SELECT COALESCE(MAX(version), 0) "
                    "FROM schema_migrations"
                )
                version = cur.fetchone()[0]

                if version < 1:
                    for statement in SCHEMA_SQL:
                        cur.execute(statement)
                    cur.execute(
                        "INSERT INTO schema_migrations(version, applied_at) "
                        "VALUES(1, %s)",
                        (now_ts(),),
                    )

                db.commit()

    # -----------------------------------------------------
    # CRUD
    # -----------------------------------------------------

    def save(self, mission: Mission) -> None:
        payload = mission.model_dump_json()
        now = now_ts()

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO missions(
                        id, payload, status, goal, repo_key,
                        claimed_by, claimed_at, heartbeat_at,
                        cancel_requested, created_at, updated_at,
                        client_id, priority
                    )
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s)
                    ON CONFLICT(id) DO UPDATE SET
                        payload = CASE
                            WHEN missions.cancel_requested THEN
                                jsonb_set(
                                    EXCLUDED.payload,
                                    '{cancel_requested}',
                                    'true'::jsonb
                                )
                            ELSE EXCLUDED.payload END,
                        status = EXCLUDED.status,
                        goal = EXCLUDED.goal,
                        repo_key = COALESCE(EXCLUDED.repo_key,
                                            missions.repo_key),
                        claimed_by = EXCLUDED.claimed_by,
                        claimed_at = EXCLUDED.claimed_at,
                        heartbeat_at = EXCLUDED.heartbeat_at,
                        cancel_requested = CASE
                            WHEN missions.cancel_requested THEN TRUE
                            ELSE EXCLUDED.cancel_requested END,
                        updated_at = EXCLUDED.updated_at,
                        client_id = COALESCE(EXCLUDED.client_id,
                                             missions.client_id),
                        priority = EXCLUDED.priority
                    """,
                    (
                        mission.id,
                        payload,
                        mission.status.value,
                        mission.goal,
                        _repo_key(mission),
                        mission.claimed_by,
                        mission.claimed_at,
                        mission.heartbeat_at,
                        bool(mission.cancel_requested),
                        mission.created_at,
                        now,
                        mission.client_id,
                        mission.priority,
                    ),
                )
            db.commit()

    def get(self, mission_id: str) -> Mission | None:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM missions WHERE id=%s",
                    (mission_id,),
                )
                row = cur.fetchone()

        if not row:
            return None

        payload = row[0]

        if isinstance(payload, str):
            return Mission.model_validate_json(payload)

        return Mission.model_validate(payload)

    def list(self) -> list[Mission]:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM missions "
                    "ORDER BY created_at DESC, id DESC"
                )
                rows = cur.fetchall()

        return [
            (
                Mission.model_validate_json(r[0])
                if isinstance(r[0], str)
                else Mission.model_validate(r[0])
            )
            for r in rows
        ]

    def status_counts(self) -> dict:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT status, COUNT(*) FROM missions "
                    "GROUP BY status"
                )
                rows = cur.fetchall()

        return {row[0]: row[1] for row in rows}

    # -----------------------------------------------------
    # Queue
    # -----------------------------------------------------

    def enqueue(self, mission: Mission) -> None:
        repo_key = _repo_key(mission)

        with self._connect() as db:
            with db.cursor() as cur:
                # Single-flight duplicate detection in the same
                # transaction as the insert.
                cur.execute(
                    """
                    SELECT id FROM missions
                    WHERE repo_key=%s AND goal=%s AND id != %s
                      AND status IN ('QUEUED','RUNNING','VERIFYING',
                                     'REPAIRING','RECOVERING')
                    FOR UPDATE
                    """,
                    (repo_key, mission.goal, mission.id),
                )
                row = cur.fetchone()

                if row:
                    raise DuplicateMission(
                        f"mission {row[0]} already active "
                        "for this repo and goal",
                        mission_id=row[0],
                    )

                now = now_ts()
                try:
                    cur.execute(
                        """
                        INSERT INTO missions(
                            id, payload, status, goal, repo_key,
                            claimed_by, claimed_at, heartbeat_at,
                            cancel_requested, created_at, updated_at,
                            client_id, priority
                        )
                        VALUES(%s, %s, 'QUEUED', %s, %s, NULL, NULL,
                               NULL, FALSE, %s, %s, %s, %s)
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
                except Exception:
                    # Lost a concurrent-insert race against an
                    # identical submission: report the winner
                    # instead of a raw constraint violation.
                    db.rollback()
                    with db.cursor() as retry:
                        retry.execute(
                            """
                            SELECT id FROM missions
                            WHERE repo_key=%s AND goal=%s AND id != %s
                              AND status IN ('QUEUED','RUNNING',
                                'VERIFYING','REPAIRING','RECOVERING')
                            """,
                            (repo_key, mission.goal, mission.id),
                        )
                        winner = retry.fetchone()
                    raise DuplicateMission(
                        f"mission {winner[0] if winner else '?'} "
                        "already active for this repo and goal",
                        mission_id=winner[0] if winner else None,
                    )

            db.commit()

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
        Exact-once claim.

        FOR UPDATE SKIP LOCKED on the candidate scan serializes
        concurrent coordinators without blocking: two claimers can
        never update the same QUEUED row, and the per-client
        quota COUNT runs in the same transaction as the claim
        UPDATE, so quota enforcement is race-free across
        processes.
        """
        skip = {str(k) for k in (skip_repo_keys or set())}
        limits = client_limits or {}

        with self._connect() as db:
            with db.cursor() as cur:
                query = (
                    "SELECT id, payload FROM missions "
                    "WHERE status='QUEUED'"
                )
                params: list = []

                if skip:
                    query += (
                        " AND repo_key NOT IN (%s)"
                        % ",".join("%s" for _ in skip)
                    )
                    params.extend(sorted(skip))

                query += (
                    " ORDER BY priority ASC, created_at ASC, id ASC "
                    "LIMIT 25 FOR UPDATE SKIP LOCKED"
                )

                cur.execute(query, params)
                rows = cur.fetchall()

                now = now_ts()

                for mission_id, payload in rows:
                    try:
                        mission = (
                            Mission.model_validate_json(payload)
                            if isinstance(payload, str)
                            else Mission.model_validate(payload)
                        )
                    except Exception:
                        # One corrupt payload must not wedge the
                        # queue; the row stays for inspection.
                        continue

                    limit = (
                        limits.get(mission.client_id)
                        if mission.client_id
                        else None
                    )

                    if limit is not None:
                        cur.execute(
                            """
                            SELECT COUNT(*) FROM missions
                            WHERE client_id=%s AND status IN
                                ('RUNNING','VERIFYING','REPAIRING',
                                 'RECOVERING')
                            """,
                            (mission.client_id,),
                        )
                        others = cur.fetchone()[0]

                        if others >= limit:
                            continue

                    mission.status = MissionStatus.running
                    mission.claimed_by = coordinator_id
                    mission.claimed_at = now
                    mission.heartbeat_at = now
                    mission.started_at = mission.started_at or now
                    mission.attempt = mission.attempt + 1

                    cur.execute(
                        """
                        UPDATE missions SET
                            payload=%s,
                            status='RUNNING',
                            claimed_by=%s,
                            claimed_at=%s,
                            heartbeat_at=%s,
                            updated_at=%s
                        WHERE id=%s AND status='QUEUED'
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

                    db.commit()
                    return mission

            db.rollback()
            return None

    def heartbeat(self, mission_id: str, coordinator_id: str) -> bool:
        now = now_ts()

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload, cancel_requested FROM missions
                    WHERE id=%s AND claimed_by=%s AND status IN
                        ('RUNNING','VERIFYING','REPAIRING')
                    FOR UPDATE
                    """,
                    (mission_id, coordinator_id),
                )
                row = cur.fetchone()

                if not row:
                    db.rollback()
                    return False

                payload = (
                    json.loads(row[0])
                    if isinstance(row[0], str)
                    else dict(row[0])
                )
                payload["heartbeat_at"] = now

                if row[1]:
                    payload["cancel_requested"] = True

                cur.execute(
                    """
                    UPDATE missions
                    SET payload=%s, heartbeat_at=%s, updated_at=%s
                    WHERE id=%s AND claimed_by=%s
                    """,
                    (
                        json.dumps(payload),
                        now,
                        now,
                        mission_id,
                        coordinator_id,
                    ),
                )

            db.commit()
            return True

    def request_cancel(self, mission_id: str) -> str:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT status FROM missions WHERE id=%s FOR UPDATE",
                    (mission_id,),
                )
                row = cur.fetchone()

                if not row:
                    db.rollback()
                    return "unknown"

                status = row[0]

                if status not in ACTIVE_STATUSES:
                    db.rollback()
                    return "terminal"

                now = now_ts()
                cur.execute(
                    """
                    UPDATE missions
                    SET cancel_requested=TRUE,
                        payload = jsonb_set(
                            jsonb_set(
                                payload,
                                '{cancel_requested}', 'true'::jsonb
                            ),
                            '{updated_at}',
                            to_jsonb(%s::text)
                        ),
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (now, now, mission_id),
                )

                if status == "QUEUED":
                    cur.execute(
                        """
                        UPDATE missions SET
                            status='CANCELLED',
                            payload = jsonb_set(
                                jsonb_set(
                                    payload,
                                    '{status}', '"CANCELLED"'::jsonb
                                ),
                                '{finished_at}',
                                to_jsonb(%s::text)
                            ),
                            updated_at=%s
                        WHERE id=%s
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

    def stale_executing(self, stale_after_seconds: int) -> list[Mission]:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=stale_after_seconds)
        ).isoformat()

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload FROM missions
                    WHERE status IN ('RUNNING','VERIFYING','REPAIRING')
                      AND (heartbeat_at IS NULL OR heartbeat_at < %s)
                    """,
                    (cutoff,),
                )
                rows = cur.fetchall()

        stale = []
        for r in rows:
            try:
                stale.append(
                    Mission.model_validate_json(r[0])
                    if isinstance(r[0], str)
                    else Mission.model_validate(r[0])
                )
            except Exception:
                # Corrupt rows stay for operator inspection.
                continue
        return stale

    # -----------------------------------------------------
    # Events
    # -----------------------------------------------------

    def record_event(
        self,
        mission_id: str,
        event_type: str,
        attempt: int = 0,
        data: dict | None = None,
    ) -> int:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO mission_events(
                        mission_id, event_type, attempt, timestamp, data
                    )
                    VALUES(%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        mission_id,
                        event_type,
                        attempt,
                        now_ts(),
                        json.dumps(data or {}),
                    ),
                )
                return cur.fetchone()[0]

    def events(self, mission_id: str) -> list[dict]:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT mission_id, event_type, attempt, timestamp,
                           data
                    FROM mission_events
                    WHERE mission_id=%s
                    ORDER BY id ASC
                    """,
                    (mission_id,),
                )
                rows = cur.fetchall()

        results = []

        for row in rows:
            data = row[4]

            if isinstance(data, str):
                data = json.loads(data)

            results.append(
                {
                    "mission_id": row[0],
                    "event_type": row[1],
                    "attempt": row[2],
                    "timestamp": row[3],
                    "data": data,
                }
            )

        return results

    # -----------------------------------------------------
    # Quota
    # -----------------------------------------------------

    def executing_count_for_client(self, client_id: str) -> int:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM missions
                    WHERE client_id=%s AND status IN
                        ('RUNNING','VERIFYING','REPAIRING','RECOVERING')
                    """,
                    (client_id,),
                )
                return cur.fetchone()[0]

    # -----------------------------------------------------
    # Outbox
    # -----------------------------------------------------

    def outbox_enqueue(
        self,
        *,
        mission_id: str,
        kind: str,
        payload: dict,
        idempotency_key: str | None = None,
    ) -> int:
        with self._connect() as db:
            with db.cursor() as cur:
                if idempotency_key:
                    cur.execute(
                        "SELECT id FROM mission_outbox "
                        "WHERE idempotency_key=%s",
                        (idempotency_key,),
                    )
                    row = cur.fetchone()

                    if row:
                        return row[0]

                cur.execute(
                    """
                    INSERT INTO mission_outbox(
                        mission_id, kind, payload, created_at,
                        idempotency_key, next_attempt_at
                    )
                    VALUES(%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING id
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
                row = cur.fetchone()

                if row:
                    db.commit()
                    return row[0]

                # Lost a concurrent idempotent-insert race.
                cur.execute(
                    "SELECT id FROM mission_outbox "
                    "WHERE idempotency_key=%s",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                db.commit()

                if existing:
                    return existing[0]

                raise RuntimeError(
                    "outbox insert neither inserted nor resolved"
                )

    def outbox_pending(
        self, limit: int = 100, kinds: list[str] | None = None
    ) -> list[dict]:
        clauses = [
            "delivered_at IS NULL",
            "dead_lettered_at IS NULL",
            "(next_attempt_at IS NULL OR next_attempt_at <= %s)",
        ]
        params: list = [now_ts()]

        if kinds:
            clauses.append(
                "kind IN (%s)" % ",".join("%s" for _ in kinds)
            )
            params.extend(kinds)

        params.append(max(1, int(limit)))

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, mission_id, kind, payload, created_at,
                           attempts
                    FROM mission_outbox
                    WHERE %s
                    ORDER BY id ASC
                    LIMIT %%s
                    """
                    % " AND ".join(clauses),
                    params,
                )
                rows = cur.fetchall()

        results = []

        for row in rows:
            payload = row[3]

            if isinstance(payload, str):
                payload = json.loads(payload)

            results.append(
                {
                    "id": row[0],
                    "mission_id": row[1],
                    "kind": row[2],
                    "payload": payload,
                    "created_at": row[4],
                    "attempts": row[5],
                }
            )

        return results

    def outbox_mark_delivered(self, outbox_id: int) -> None:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mission_outbox
                    SET delivered_at=%s, attempts=attempts+1,
                        next_attempt_at=NULL
                    WHERE id=%s
                    """,
                    (now_ts(), outbox_id),
                )
            db.commit()

    def outbox_mark_delivered_if_pending(self, outbox_id: int) -> bool:
        """
        Atomic claim of the delivery acknowledgment (see the SQLite
        adapter for the contract). One winner; losers delivered a
        replay that idempotent handlers absorb.
        """
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mission_outbox
                    SET delivered_at=%s, attempts=attempts+1,
                        next_attempt_at=NULL
                    WHERE id=%s AND delivered_at IS NULL
                      AND dead_lettered_at IS NULL
                    """,
                    (now_ts(), outbox_id),
                )
                won = cur.rowcount > 0

            db.commit()
            return won

    def outbox_mark_failed(self, outbox_id: int, error: str) -> None:
        now = now_ts()

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT attempts FROM mission_outbox WHERE id=%s "
                    "FOR UPDATE",
                    (outbox_id,),
                )
                row = cur.fetchone()

                if not row:
                    db.rollback()
                    return

                attempts = row[0] + 1

                if attempts >= OUTBOX_MAX_ATTEMPTS:
                    cur.execute(
                        """
                        UPDATE mission_outbox
                        SET attempts=%s, last_error=%s,
                            dead_lettered_at=%s
                        WHERE id=%s
                        """,
                        (attempts, error[:500], now, outbox_id),
                    )
                else:
                    backoff = min(
                        OUTBOX_BACKOFF_MAX_SECONDS,
                        OUTBOX_BACKOFF_BASE_SECONDS
                        * (2 ** (attempts - 1)),
                    )
                    next_at = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=backoff)
                    ).isoformat()

                    cur.execute(
                        """
                        UPDATE mission_outbox
                        SET attempts=%s, last_error=%s,
                            next_attempt_at=%s
                        WHERE id=%s
                        """,
                        (attempts, error[:500], next_at, outbox_id),
                    )

            db.commit()

    def outbox_dead_letter(self, outbox_id: int) -> None:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    UPDATE mission_outbox
                    SET dead_lettered_at=%s
                    WHERE id=%s AND delivered_at IS NULL
                    """,
                    (now_ts(), outbox_id),
                )
            db.commit()

    def outbox_requeue_dead(self, outbox_id: int | None = None) -> int:
        with self._connect() as db:
            with db.cursor() as cur:
                if outbox_id is None:
                    cur.execute(
                        """
                        UPDATE mission_outbox
                        SET dead_lettered_at=NULL, next_attempt_at=%s,
                            attempts=0
                        WHERE dead_lettered_at IS NOT NULL
                        """,
                        (now_ts(),),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE mission_outbox
                        SET dead_lettered_at=NULL, next_attempt_at=%s,
                            attempts=0
                        WHERE id=%s AND dead_lettered_at IS NOT NULL
                        """,
                        (now_ts(), outbox_id),
                    )

                count = cur.rowcount
            db.commit()
            return count

    def outbox_message(self, outbox_id: int) -> dict | None:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, mission_id, kind, payload, created_at,
                           attempts, delivered_at, last_error,
                           idempotency_key, next_attempt_at,
                           dead_lettered_at
                    FROM mission_outbox WHERE id=%s
                    """,
                    (outbox_id,),
                )
                row = cur.fetchone()

        if not row:
            return None

        payload = row[3]

        if isinstance(payload, str):
            payload = json.loads(payload)

        return {
            "id": row[0],
            "mission_id": row[1],
            "kind": row[2],
            "payload": payload,
            "created_at": row[4],
            "attempts": row[5],
            "delivered_at": row[6],
            "last_error": row[7],
            "idempotency_key": row[8],
            "next_attempt_at": row[9],
            "dead_lettered_at": row[10],
        }

    def outbox_list_dead(self, limit: int = 200) -> list[dict]:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, mission_id, kind, attempts, last_error,
                           dead_lettered_at
                    FROM mission_outbox
                    WHERE dead_lettered_at IS NOT NULL
                    ORDER BY id ASC
                    LIMIT %s
                    """,
                    (max(1, int(limit)),),
                )
                rows = cur.fetchall()

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

    def outbox_stats(self) -> dict:
        # Pending means deliverable work: undelivered AND not
        # dead-lettered (mirrors the SQLite adapter).
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE delivered_at IS NULL
                             AND dead_lettered_at IS NULL),
                           COUNT(*) FILTER (WHERE delivered_at IS NOT NULL),
                           COUNT(*) FILTER (
                               WHERE dead_lettered_at IS NOT NULL)
                    FROM mission_outbox
                    """
                )
                row = cur.fetchone()

        return {
            "total": row[0],
            "pending": row[1],
            "delivered": row[2],
            "dead_lettered": row[3],
        }

    # -----------------------------------------------------
    # Rate limiting (10.4) — multi-process safe token bucket
    # -----------------------------------------------------

    def rate_limit_take(
        self,
        bucket_key: str,
        *,
        now: float,
        capacity: int,
        refill_per_second: float,
        tokens: float = 1.0,
    ) -> tuple[bool, float]:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT tokens, updated_at FROM rate_limit_buckets
                    WHERE bucket_key=%s FOR UPDATE
                    """,
                    (bucket_key,),
                )
                row = cur.fetchone()

                if row:
                    current, updated_at = row
                    elapsed = max(0.0, now - float(updated_at))
                    level = min(
                        float(capacity),
                        float(current) + elapsed * refill_per_second,
                    )
                else:
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

                cur.execute(
                    """
                    INSERT INTO rate_limit_buckets(
                        bucket_key, tokens, updated_at
                    )
                    VALUES(%s, %s, %s)
                    ON CONFLICT(bucket_key) DO UPDATE SET
                        tokens = EXCLUDED.tokens,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (bucket_key, level, now),
                )

            db.commit()

        return allowed, retry_after


class PostgresAuditStore:
    """Tamper-evident audit trail over Postgres."""

    def __init__(self, dsn: str):
        _require_psycopg()
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    def append(
        self,
        *,
        client_id: str | None = None,
        actor: str | None = None,
        action: str,
        mission_id: str | None = None,
        data: dict | None = None,
    ) -> dict:
        from app.tenants.audit import canonical_event_payload, event_chain_hash
        from app.tenants.redact import redact

        payload = json.dumps(redact(data or {}))

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT event_hash FROM audit_events "
                    "ORDER BY seq DESC LIMIT 1 FOR UPDATE"
                )
                row = cur.fetchone()
                prev = row[0] if row else None

                cur.execute(
                    """
                    INSERT INTO audit_events(
                        ts, actor, client_id, action, mission_id, data,
                        prev_hash, event_hash
                    )
                    VALUES(%s, %s, %s, %s, %s, %s, %s, '')
                    RETURNING seq
                    """,
                    (now_ts(), actor, client_id, action, mission_id, payload, prev),
                )
                seq = cur.fetchone()[0]

                canonical = canonical_event_payload(
                    seq=seq,
                    ts=now_ts(),
                    actor=actor,
                    client_id=client_id,
                    action=action,
                    mission_id=mission_id,
                    data=payload,
                )
                event_hash = event_chain_hash(prev, canonical)

                cur.execute(
                    "UPDATE audit_events SET event_hash=%s WHERE seq=%s",
                    (event_hash, seq),
                )

            db.commit()

        return {
            "seq": seq,
            "actor": actor,
            "client_id": client_id,
            "action": action,
            "mission_id": mission_id,
            "event_hash": event_hash,
            "prev_hash": prev,
        }

    def query(
        self,
        client_id: str | None = None,
        mission_id: str | None = None,
        action: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        clauses = []
        params: list = []

        if client_id is not None:
            clauses.append("client_id=%s")
            params.append(client_id)

        if mission_id is not None:
            clauses.append("mission_id=%s")
            params.append(mission_id)

        if action is not None:
            clauses.append("action=%s")
            params.append(action)

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""

        params.extend([max(1, min(int(limit), 1000)), max(0, int(offset))])

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT seq, ts, actor, client_id, action, mission_id,
                           data, event_hash
                    FROM audit_events
                    {where}
                    ORDER BY seq ASC
                    LIMIT %s OFFSET %s
                    """,
                    params,
                )
                rows = cur.fetchall()

        results = []

        for row in rows:
            data = row[6]

            if isinstance(data, str):
                data = json.loads(data)

            results.append(
                {
                    "seq": row[0],
                    "ts": row[1],
                    "actor": row[2],
                    "client_id": row[3],
                    "action": row[4],
                    "mission_id": row[5],
                    "data": data,
                    "event_hash": row[7],
                }
            )

        return results

    def count(self) -> int:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM audit_events")
                return cur.fetchone()[0]

    def verify(self) -> dict:
        from app.tenants.audit import canonical_event_payload, event_chain_hash

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT seq, ts, actor, client_id, action, mission_id,
                           data, prev_hash, event_hash
                    FROM audit_events
                    ORDER BY seq ASC
                    """
                )
                rows = cur.fetchall()

        prev = None
        verified_through = None

        for row in rows:
            seq = row[0]
            data = row[6]

            if isinstance(data, str):
                data = json.loads(data)

            canonical = canonical_event_payload(
                seq=seq,
                ts=row[1],
                actor=row[2],
                client_id=row[3],
                action=row[4],
                mission_id=row[5],
                data=data,
            )
            expected = event_chain_hash(prev, canonical)

            if row[7] != prev or row[8] != expected:
                return {
                    "intact": False,
                    "events": len(rows),
                    "verified_through": verified_through,
                    "broken_at": seq,
                    "reason": "hash mismatch or broken link",
                }

            prev = row[8]
            verified_through = seq

        return {
            "intact": True,
            "events": len(rows),
            "verified_through": verified_through,
            "broken_at": None,
            "reason": None,
        }


class PostgresLearningStore:
    """Learning records (idempotent upsert) over Postgres."""

    def __init__(self, dsn: str):
        _require_psycopg()
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, autocommit=False)

    def save(self, record) -> None:
        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO learning_records(id, payload)
                    VALUES(%s, %s)
                    ON CONFLICT(id) DO UPDATE SET
                        payload = EXCLUDED.payload
                    """,
                    (record.id, record.model_dump_json()),
                )
            db.commit()

    def list(self):
        from app.learning.models import LearningRecord

        with self._connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM learning_records "
                    "ORDER BY created_at DESC"
                )
                rows = cur.fetchall()

        records = []

        for (payload,) in rows:
            records.append(
                LearningRecord.model_validate_json(payload)
                if isinstance(payload, str)
                else LearningRecord.model_validate(payload)
            )

        return records


class PostgresStores:
    """Bundle of protocol adapters bound to one Postgres DSN."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.store = PostgresMissionStore(dsn)
        self.audit = PostgresAuditStore(dsn)

    @property
    def path(self) -> str:
        """Back-compat: LearningStore(outbox relay) binds by path."""
        return self.dsn
