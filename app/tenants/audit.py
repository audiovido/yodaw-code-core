"""
Stage 9.3: append-only audit trail.

Every tenant-visible mutation is recorded as an audit event in
one immediate transaction. Rows are never updated or deleted:
compliance review relies on the trail being immutable.

Secrets are redacted at the boundary (see redact.py): a raw API
key or bearer token must never reach an audit row.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.storage.db import DB_PATH
from app.tenants.redact import redact


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_table(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            client_id TEXT,
            action TEXT NOT NULL,
            mission_id TEXT,
            data TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_client_seq "
        "ON audit_events(client_id, seq)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_mission "
        "ON audit_events(mission_id, seq)"
    )


class AuditStore:
    """
    Append-only audit trail with a SQLite-backed write lock.

    The lock serializes writers within this process so the
    AUTOINCREMENT sequence is strictly monotonic in append order
    even under concurrent requests; SQLite's write lock covers
    cross-process ordering.
    """

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        with sqlite3.connect(self.path) as db:
            _ensure_table(db)

    def append(
        self,
        *,
        client_id: str | None,
        action: str,
        mission_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        payload = json.dumps(redact(data or {}))

        with self._lock, sqlite3.connect(self.path) as db:
            db.execute(
                """
                INSERT INTO audit_events(
                    ts, client_id, action, mission_id, data
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    now_ts(),
                    client_id,
                    action,
                    mission_id,
                    payload,
                ),
            )

    def query(
        self,
        client_id: str | None = None,
        mission_id: str | None = None,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        """
        Read the trail. Order is global append order (seq ASC) so
        any filtered view stays chronologically consistent.
        """
        clauses = []
        params: list = []

        if client_id is not None:
            clauses.append("client_id=?")
            params.append(client_id)

        if mission_id is not None:
            clauses.append("mission_id=?")
            params.append(mission_id)

        if action is not None:
            clauses.append("action=?")
            params.append(action)

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""

        params.append(max(1, min(int(limit), 1000)))

        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                f"""
                SELECT seq, ts, client_id, action, mission_id, data
                FROM audit_events
                {where}
                ORDER BY seq ASC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [
            {
                "seq": row[0],
                "ts": row[1],
                "client_id": row[2],
                "action": row[3],
                "mission_id": row[4],
                "data": json.loads(row[5]),
            }
            for row in rows
        ]

    def count(self) -> int:
        with sqlite3.connect(self.path) as db:
            return db.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0]
