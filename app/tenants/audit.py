"""
Stage 9.3 + Stage 10.5: append-only, tamper-evident audit trail.

Every tenant-visible mutation is recorded as an audit event.
Rows are never updated in place and never deleted except by the
explicit retention prune/archive command.

Tamper evidence (10.5):

    event_hash = SHA-256(prev_hash || canonical_event_payload)

Each row commits to the full previous history, so any mutation,
deletion, reordering, or insertion into the stored rows breaks
the chain and `verify()` names the first broken sequence number.

This is tamper-EVIDENT integrity for operational monitoring, not
cryptographic non-repudiation: an attacker with database write
access can recompute a whole chain. It reliably detects local
accidents, partial writes, and unsophisticated tampering.

Secrets are redacted at the boundary (see redact.py): a raw API
key or bearer token must never reach an audit row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage.db import DB_PATH
from app.tenants.redact import redact


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_event_payload(
    *,
    seq: int,
    ts: str,
    actor: str | None,
    client_id: str | None,
    action: str,
    mission_id: str | None,
    data: str | dict,
) -> str:
    """
    Deterministic JSON serialization of one event's signed fields.

    `data` is accepted as the stored JSON text (migration path) or
    as a dict (append path); both normalize to the same canonical
    form so a chain built in either way verifies identically.
    """
    if isinstance(data, str):
        data = json.loads(data) if data else {}

    payload = {
        "seq": int(seq),
        "ts": ts,
        "actor": actor,
        "client_id": client_id,
        "action": action,
        "mission_id": mission_id,
        "data": data,
    }

    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def event_chain_hash(prev_hash: str | None, canonical: str) -> str:
    """SHA-256(prev_hash || canonical_event_payload)."""
    digest = hashlib.sha256()
    digest.update((prev_hash or "").encode("utf-8"))
    digest.update(canonical.encode("utf-8"))
    return digest.hexdigest()


def _ensure_table(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT,
            client_id TEXT,
            action TEXT NOT NULL,
            mission_id TEXT,
            data TEXT NOT NULL DEFAULT '{}',
            prev_hash TEXT,
            event_hash TEXT
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
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_ts "
        "ON audit_events(ts)"
    )
    # Chain-verify walks seq order; make the head lookup cheap.
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_seq_hash "
        "ON audit_events(seq, event_hash)"
    )
    # Retention boundaries: after a prune, the first surviving row
    # still links (prev_hash) to the pruned segment's head. The
    # anchor row records that head so verify() can confirm the
    # linkage instead of reporting a false break.
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_chain_anchors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            up_to_seq INTEGER NOT NULL,
            head_hash TEXT NOT NULL,
            pruned_at TEXT NOT NULL
        )
        """
    )


class AuditStore:
    """
    Append-only audit trail with a SQLite-backed write lock.

    The lock serializes writers within this process so the
    AUTOINCREMENT sequence is strictly monotonic in append order
    and each row's prev_hash is the hash of the row appended
    immediately before it, even under concurrent requests;
    SQLite's write lock covers cross-process ordering.
    """

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        with sqlite3.connect(self.path) as db:
            _ensure_table(db)

    # -----------------------------------------------------
    # Append
    # -----------------------------------------------------

    def append(
        self,
        *,
        client_id: str | None = None,
        actor: str | None = None,
        action: str,
        mission_id: str | None = None,
        data: dict | None = None,
    ) -> dict:
        """
        Append one event and link it into the hash chain.

        Returns the stored event (seq, ts, actor, client_id,
        action, mission_id, data, event_hash). The caller-supplied
        data is redacted before persistence; secrets cannot reach
        the row.
        """
        payload = json.dumps(redact(data or {}))

        with self._lock, sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute(
                """
                SELECT event_hash FROM audit_events
                ORDER BY seq DESC LIMIT 1
                """
            ).fetchone()

            prev = row[0] if row else None

            cursor = db.execute(
                """
                INSERT INTO audit_events(
                    ts, actor, client_id, action, mission_id, data,
                    prev_hash, event_hash
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ts(),
                    actor,
                    client_id,
                    action,
                    mission_id,
                    payload,
                    prev,
                    "",  # filled right below; needs the seq
                ),
            )

            seq = cursor.lastrowid

            canonical = canonical_event_payload(
                seq=seq,
                ts=db.execute(
                    "SELECT ts FROM audit_events WHERE seq=?", (seq,)
                ).fetchone()[0],
                actor=actor,
                client_id=client_id,
                action=action,
                mission_id=mission_id,
                data=payload,
            )

            event_hash = event_chain_hash(prev, canonical)

            db.execute(
                "UPDATE audit_events SET event_hash=? WHERE seq=?",
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

    # -----------------------------------------------------
    # Query
    # -----------------------------------------------------

    def query(
        self,
        client_id: str | None = None,
        mission_id: str | None = None,
        action: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """
        Read the trail. Order is global append order (seq ASC) so
        any filtered view stays chronologically consistent.

        Stage 10.5: offset/limit pagination for bounded responses.
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

        params.extend(
            [max(1, min(int(limit), 1000)), max(0, int(offset))]
        )

        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                f"""
                SELECT seq, ts, actor, client_id, action, mission_id, data,
                       event_hash
                FROM audit_events
                {where}
                ORDER BY seq ASC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()

        return [
            {
                "seq": row[0],
                "ts": row[1],
                "actor": row[2],
                "client_id": row[3],
                "action": row[4],
                "mission_id": row[5],
                "data": json.loads(row[6]),
                "event_hash": row[7],
            }
            for row in rows
        ]

    def count(self) -> int:
        with sqlite3.connect(self.path) as db:
            return db.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0]

    # -----------------------------------------------------
    # Tamper evidence (10.5)
    # -----------------------------------------------------

    def verify(self) -> dict:
        """
        Walk the chain and report the first inconsistency.

        Returns {intact, events, verified_through, broken_at,
        reason}. verified_through is the last seq whose hash
        matched its predecessor (the chain is valid up to and
        including it).
        """
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                """
                SELECT seq, ts, actor, client_id, action, mission_id,
                       data, prev_hash, event_hash
                FROM audit_events
                ORDER BY seq ASC
                """
            ).fetchall()

        anchors = self._anchors()
        anchor_hashes = {a["head_hash"] for a in anchors}

        prev = None
        verified_through = None

        for row in rows:
            seq, ts, actor, client_id, action, mission_id, data, \
                stored_prev, stored_hash = row

            if prev is None and stored_prev is not None:
                # First surviving row links to pruned history;
                # accept only when a recorded retention boundary
                # anchors that link, then resume the chain from
                # the anchor hash. A missing or forged anchor
                # fails verification.
                if stored_prev not in anchor_hashes:
                    return {
                        "intact": False,
                        "events": len(rows),
                        "verified_through": None,
                        "broken_at": seq,
                        "reason": "first row links to unknown history",
                    }

                prev = stored_prev

            canonical = canonical_event_payload(
                seq=seq,
                ts=ts,
                actor=actor,
                client_id=client_id,
                action=action,
                mission_id=mission_id,
                data=data,
            )
            expected = event_chain_hash(prev, canonical)

            if stored_prev != prev or stored_hash != expected:
                return {
                    "intact": False,
                    "events": len(rows),
                    "verified_through": verified_through,
                    "broken_at": seq,
                    "reason": "hash mismatch or broken link",
                }

            prev = stored_hash
            verified_through = seq

        return {
            "intact": True,
            "events": len(rows),
            "verified_through": verified_through,
            "broken_at": None,
            "reason": None,
        }

    def _anchors(self) -> list[dict]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                """
                SELECT up_to_seq, head_hash, pruned_at
                FROM audit_chain_anchors
                ORDER BY up_to_seq ASC
                """
            ).fetchall()

        return [
            {
                "up_to_seq": row[0],
                "head_hash": row[1],
                "pruned_at": row[2],
            }
            for row in rows
        ]

    # -----------------------------------------------------
    # Retention (10.5)
    # -----------------------------------------------------

    def prune(
        self,
        *,
        keep_days: int,
        archive_path: Path | str | None = None,
    ) -> dict:
        """
        Delete events older than keep_days, optionally archiving
        them first as JSON lines.

        The boundary is preserved: the archived file ends with the
        chain head of the pruned range, and the deletion keeps the
        remaining chain intact (the oldest surviving row keeps its
        own prev_hash/event_hash — the chain of the surviving
        segment verifies on its own from its first row onward,
        which verify() reports as intact).

        Returns {pruned, archived, head_hash, head_seq}.
        """
        if keep_days < 1:
            raise ValueError("keep_days must be >= 1")

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=int(keep_days))
        ).isoformat()

        with self._lock, sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")

            rows = db.execute(
                """
                SELECT seq, ts, actor, client_id, action, mission_id,
                       data, prev_hash, event_hash
                FROM audit_events WHERE ts < ?
                ORDER BY seq ASC
                """,
                (cutoff,),
            ).fetchall()

            if not rows:
                db.rollback()

                head = db.execute(
                    "SELECT seq, event_hash FROM audit_events "
                    "ORDER BY seq DESC LIMIT 1"
                ).fetchone()

                return {
                    "pruned": 0,
                    "archived": 0,
                    "head_seq": head[0] if head else None,
                    "head_hash": head[1] if head else None,
                }

            head_seq = rows[-1][0]
            head_hash = rows[-1][8]

            if archive_path is not None:
                archive = Path(archive_path)
                archive.parent.mkdir(parents=True, exist_ok=True)

                with archive.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "boundary": {
                                    "pruned_through_seq": head_seq,
                                    "pruned_through_hash": head_hash,
                                    "cutoff": cutoff,
                                    "archived_at": now_ts(),
                                }
                            }
                        )
                        + "\n"
                    )
                    for row in rows:
                        fh.write(
                            json.dumps(
                                {
                                    "seq": row[0],
                                    "ts": row[1],
                                    "actor": row[2],
                                    "client_id": row[3],
                                    "action": row[4],
                                    "mission_id": row[5],
                                    "data": json.loads(row[6]),
                                    "prev_hash": row[7],
                                    "event_hash": row[8],
                                }
                            )
                            + "\n"
                        )

            db.execute(
                "DELETE FROM audit_events WHERE ts < ?", (cutoff,)
            )
            db.execute(
                """
                INSERT INTO audit_chain_anchors(
                    up_to_seq, head_hash, pruned_at
                )
                VALUES(?, ?, ?)
                """,
                (head_seq, head_hash, now_ts()),
            )
            db.commit()

        return {
            "pruned": len(rows),
            "archived": len(rows) if archive_path is not None else 0,
            "head_seq": head_seq,
            "head_hash": head_hash,
        }
