"""
Stage 9.1: API client identities.

A client is a caller identity with an API key, quota limits, and
a queue priority. Keys are stored hashed (SHA-256): the plaintext
key is returned exactly once at creation and never persisted.

Identities live in their own table with their own migration
version counter, so the missions table and Stage 7/8 behavior
remain untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.storage.db import DB_PATH


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_key(plaintext: str) -> str:
    """SHA-256 of the plaintext key. Keys are never stored raw."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _ensure_table(db) -> None:
    db.execute(
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
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_clients_key_hash "
        "ON api_clients(key_hash)"
    )


def _migrate(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tenants_schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )

    # Own version counter so mission-store migrations (which own
    # PRAGMA user_version) stay fully independent.
    tenant_version = db.execute(
        "SELECT COALESCE(MAX(version), 0) FROM tenants_schema_migrations"
    ).fetchone()[0]

    if tenant_version < 1:
        _ensure_table(db)
        db.execute(
            "INSERT INTO tenants_schema_migrations(version, applied_at) "
            "VALUES(1, ?)",
            (now_ts(),),
        )


class ClientRecord:
    """Resolved identity for one authenticated request."""

    __slots__ = (
        "id",
        "name",
        "priority",
        "max_concurrent_missions",
        "disabled",
    )

    def __init__(
        self,
        id: str,
        name: str,
        priority: int,
        max_concurrent_missions: int | None,
        disabled: bool,
    ):
        self.id = id
        self.name = name
        self.priority = priority
        self.max_concurrent_missions = max_concurrent_missions
        self.disabled = disabled

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "priority": self.priority,
            "max_concurrent_missions": self.max_concurrent_missions,
            "disabled": self.disabled,
        }


class ClientStore:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.path) as db:
            _migrate(db)

    # -----------------------------------------------------
    # Admin operations
    # -----------------------------------------------------

    def create_client(
        self,
        name: str,
        priority: int = 5,
        max_concurrent_missions: int | None = None,
    ) -> dict:
        """
        Create a client identity.

        Returns the plaintext key exactly once; only its hash is
        persisted. 1 (highest) <= priority <= 9 (lowest).
        """
        plaintext = f"yodak_{secrets.token_urlsafe(24)}"

        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")

            try:
                db.execute(
                    """
                    INSERT INTO api_clients(
                        id, name, key_hash, priority,
                        max_concurrent_missions, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"cl_{secrets.token_hex(6)}",
                        name,
                        hash_key(plaintext),
                        max(1, min(9, int(priority))),
                        max_concurrent_missions,
                        now_ts(),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ValueError(f"client name taken: {name}") from exc

        return {
            "name": name,
            "api_key": plaintext,
            "priority": max(1, min(9, int(priority))),
            "max_concurrent_missions": max_concurrent_missions,
        }

    def set_disabled(self, name: str, disabled: bool) -> bool:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                """
                UPDATE api_clients
                SET disabled_at=?
                WHERE name=?
                """,
                (now_ts() if disabled else None, name),
            )
            return cursor.rowcount > 0

    def set_priority(self, name: str, priority: int) -> bool:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                """
                UPDATE api_clients
                SET priority=?
                WHERE name=?
                """,
                (max(1, min(9, int(priority))), name),
            )
            return cursor.rowcount > 0

    def list_clients(self) -> list[dict]:
        """Admin listing: never includes key hashes."""
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                """
                SELECT id, name, priority, max_concurrent_missions,
                       created_at, disabled_at
                FROM api_clients
                ORDER BY created_at ASC
                """
            ).fetchall()

        return [
            {
                "id": row[0],
                "name": row[1],
                "priority": row[2],
                "max_concurrent_missions": row[3],
                "created_at": row[4],
                "disabled": row[5] is not None,
            }
            for row in rows
        ]

    # -----------------------------------------------------
    # Request-path resolution
    # -----------------------------------------------------

    def authenticate(self, plaintext_key: str) -> ClientRecord | None:
        """
        Resolve a bearer key to a client identity.

        Lookup is by SHA-256 hash; comparison is constant-time over
        the digest. Disabled clients resolve to None (401), so a
        revoked key is indistinguishable from an unknown one.
        """
        if not plaintext_key:
            return None

        digest = hash_key(plaintext_key)

        with sqlite3.connect(self.path) as db:
            row = db.execute(
                """
                SELECT id, name, priority, max_concurrent_missions,
                       disabled_at
                FROM api_clients
                WHERE key_hash=?
                """,
                (digest,),
            ).fetchone()

        if not row:
            return None

        record = ClientRecord(
            id=row[0],
            name=row[1],
            priority=row[2],
            max_concurrent_missions=row[3],
            disabled=row[4] is not None,
        )

        if record.disabled:
            return None

        # Constant-time confirmation over the digest (defense in
        # depth; the indexed lookup already keyed equality).
        if not hmac.compare_digest(digest, hash_key(plaintext_key)):
            return None

        return record

    def count_clients(self) -> int:
        with sqlite3.connect(self.path) as db:
            return db.execute(
                "SELECT COUNT(*) FROM api_clients"
            ).fetchone()[0]
