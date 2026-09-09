"""
Stage 10.3: admin identities and role-based access control.

Roles (minimum set):

- superadmin: manage admins, manage clients, set priorities and
  quotas, view audit, runtime controls
- operator:   inspect missions/runtime; operational retry/cancel;
              no admin or client identity management
- auditor:    read audit/events; no mutations at all
- client:     own missions only; no administrative endpoints
              (clients remain in the Stage 9 client store — the
              "client" role here names that caller class in the
              permission model)

Admin API keys are hashed (SHA-256) exactly like client keys; the
plaintext `yodad_...` key is returned exactly once at creation or
rotation and never persisted.

Legacy Stage 8 compatibility: YODAW_API_KEY maps to a virtual
superadmin identity in the authorization layer (see app/api/auth.py)
until operators migrate to real admin identities. The mapping is
deprecated and a configuration warning is emitted; the Stage 9
local-dev open mode is refused entirely under the production
profile (see app/config.py).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.storage.db import DB_PATH

ROLES = ("superadmin", "operator", "auditor")

VALID_ROLES = set(ROLES)


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_key(plaintext: str) -> str:
    """SHA-256 of the plaintext key. Keys are never stored raw."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _ensure_table(db) -> None:
    db.execute(
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
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_admins_key_hash "
        "ON api_admins(key_hash)"
    )


class AdminRecord:
    """Resolved identity for one authenticated admin request."""

    __slots__ = ("id", "name", "role", "disabled")

    def __init__(self, id: str, name: str, role: str, disabled: bool):
        self.id = id
        self.name = name
        self.role = role
        self.disabled = disabled

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "disabled": self.disabled,
        }


class AdminStore:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.path) as db:
            _ensure_table(db)

    # -----------------------------------------------------
    # Admin operations (superadmin surface)
    # -----------------------------------------------------

    def create_admin(self, name: str, role: str) -> dict:
        """
        Create an admin identity.

        Returns the plaintext key exactly once; only its hash is
        persisted. Role must be one of the RBAC roles.
        """
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")

        plaintext = f"yodad_{secrets.token_urlsafe(24)}"
        admin_id = f"ad_{secrets.token_hex(6)}"

        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")

            try:
                db.execute(
                    """
                    INSERT INTO api_admins(
                        id, name, key_hash, role, created_at
                    )
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (
                        admin_id,
                        name,
                        hash_key(plaintext),
                        role,
                        now_ts(),
                    ),
                )
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise ValueError(f"admin name taken: {name}") from exc

        return {
            "name": name,
            "role": role,
            "api_key": plaintext,
        }

    def rotate_key(self, name: str) -> dict | None:
        """
        Key rotation: issue a new plaintext key for an existing
        admin. The old key stops working the moment the new hash
        is committed; the new plaintext is shown exactly once.
        """
        plaintext = f"yodad_{secrets.token_urlsafe(24)}"

        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                """
                UPDATE api_admins
                SET key_hash=?, rotated_at=?
                WHERE name=? AND disabled_at IS NULL
                """,
                (hash_key(plaintext), now_ts(), name),
            )

            if cursor.rowcount == 0:
                return None

            db.commit()

        return {"name": name, "api_key": plaintext}

    def set_role(self, name: str, role: str) -> bool:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role}")

        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "UPDATE api_admins SET role=? WHERE name=?",
                (role, name),
            )
            db.commit()
            return cursor.rowcount > 0

    def set_disabled(self, name: str, disabled: bool) -> bool:
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                """
                UPDATE api_admins
                SET disabled_at=?
                WHERE name=?
                """,
                (now_ts() if disabled else None, name),
            )
            db.commit()
            return cursor.rowcount > 0

    def list_admins(self) -> list[dict]:
        """Admin listing: never includes key hashes."""
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                """
                SELECT id, name, role, created_at, rotated_at,
                       disabled_at
                FROM api_admins
                ORDER BY created_at ASC
                """
            ).fetchall()

        return [
            {
                "id": row[0],
                "name": row[1],
                "role": row[2],
                "created_at": row[3],
                "rotated_at": row[4],
                "disabled": row[5] is not None,
            }
            for row in rows
        ]

    def count_admins(self) -> int:
        with sqlite3.connect(self.path) as db:
            return db.execute(
                "SELECT COUNT(*) FROM api_admins"
            ).fetchone()[0]

    # -----------------------------------------------------
    # Request-path resolution
    # -----------------------------------------------------

    def authenticate(self, plaintext_key: str) -> AdminRecord | None:
        """
        Resolve a bearer key to an admin identity.

        Lookup is by SHA-256 hash; disabled admins resolve to None
        (401), indistinguishable from unknown keys.
        """
        if not plaintext_key:
            return None

        digest = hash_key(plaintext_key)

        with sqlite3.connect(self.path) as db:
            row = db.execute(
                """
                SELECT id, name, role, disabled_at
                FROM api_admins
                WHERE key_hash=?
                """,
                (digest,),
            ).fetchone()

        if not row:
            return None

        record = AdminRecord(
            id=row[0],
            name=row[1],
            role=row[2],
            disabled=row[3] is not None,
        )

        if record.disabled:
            return None

        # Constant-time confirmation over the digest (defense in
        # depth; the indexed lookup already keyed equality).
        if not hmac.compare_digest(digest, hash_key(plaintext_key)):
            return None

        return record
