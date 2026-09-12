"""
Stage 8.3: per-repository exclusion leases.

SQLite-backed lease table providing cross-process mutual
exclusion per target repository. Different repositories run
concurrently; the same repository never has two executing
missions, no matter how many coordinator processes or worker
threads exist.

Leases expire (stale heartbeat) so a crashed coordinator cannot
lock a repository forever.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from app.storage.db import DB_PATH, connect


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_table(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_leases (
            repo_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            mission_id TEXT,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        )
        """
    )


class RepoLeaseManager:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with connect(self.path) as db:
            _ensure_table(db)

    def acquire(
        self,
        repo_key: str,
        owner: str,
        mission_id: Optional[str] = None,
        stale_after_seconds: int = 300,
    ) -> bool:
        """
        Try to take the lease for this repo.

        Returns True on success, False if another live owner holds
        it. A lease whose heartbeat is older than the cutoff is
        considered stale and is stolen safely. Re-acquiring a lease
        this owner already holds (same or different mission) is
        allowed; the caller guarantees single-flight per mission.
        """
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=stale_after_seconds)
        ).isoformat()

        now = now_ts()

        db = connect(self.path)
        try:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT owner, heartbeat_at FROM repo_leases
                WHERE repo_key=?
                """,
                (repo_key,),
            ).fetchone()

            if row and row[0] != owner and row[1] >= cutoff:
                db.rollback()
                return False

            db.execute(
                """
                INSERT INTO repo_leases(
                    repo_key, owner, mission_id,
                    acquired_at, heartbeat_at
                )
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(repo_key) DO UPDATE SET
                    owner=excluded.owner,
                    mission_id=excluded.mission_id,
                    acquired_at=excluded.acquired_at,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (repo_key, owner, mission_id, now, now),
            )

            db.commit()
            return True

        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def heartbeat(self, repo_key: str, owner: str) -> bool:
        now = now_ts()

        with connect(self.path) as db:
            cursor = db.execute(
                """
                UPDATE repo_leases
                SET heartbeat_at=?
                WHERE repo_key=? AND owner=?
                """,
                (now, repo_key, owner),
            )
            return cursor.rowcount > 0

    def release(self, repo_key: str, owner: str) -> None:
        with connect(self.path) as db:
            db.execute(
                "DELETE FROM repo_leases WHERE repo_key=? AND owner=?",
                (repo_key, owner),
            )

    def release_all(self, owner: str) -> None:
        with connect(self.path) as db:
            db.execute(
                "DELETE FROM repo_leases WHERE owner=?",
                (owner,),
            )

    def held_by(self, owner: str) -> set[str]:
        with connect(self.path) as db:
            rows = db.execute(
                "SELECT repo_key FROM repo_leases WHERE owner=?",
                (owner,),
            ).fetchall()

        return {row[0] for row in rows}

    def holder(self, repo_key: str) -> Optional[str]:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT owner FROM repo_leases WHERE repo_key=?",
                (repo_key,),
            ).fetchone()

        return row[0] if row else None
