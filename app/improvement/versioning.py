"""Versioned activation and rollback for approved proposals.

Activation is versioned: each activation records a monotonically
increasing version number, the proposal it activated, the previous
active version (lineage), and the actor. Only APPROVED proposals can
activate — anything else raises, which is what makes silent mutation
impossible. Rollback deactivates the current version and restores the
previous one; the full history is retained for audit.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from app.improvement.models import ImprovementProposal, ProposalStatus, now_iso
from app.improvement.store import ProposalStore
from app.storage.db import DB_PATH


def _ensure_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS improvement_activations (
            version INTEGER PRIMARY KEY,
            proposal_id TEXT NOT NULL,
            previous_version INTEGER,
            actor TEXT NOT NULL DEFAULT 'loop',
            active INTEGER NOT NULL DEFAULT 1,
            activated_at TEXT NOT NULL,
            rolled_back_at TEXT
        )
        """
    )


@dataclass
class ActivationRecord:
    version: int
    proposal_id: str
    previous_version: int | None
    actor: str
    active: bool
    activated_at: str
    rolled_back_at: str | None = None


def _row_to_record(row: sqlite3.Row) -> ActivationRecord:
    data = dict(row)
    return ActivationRecord(
        version=int(data["version"]),
        proposal_id=str(data["proposal_id"]),
        previous_version=(
            int(data["previous_version"])
            if data.get("previous_version") is not None
            else None
        ),
        actor=str(data.get("actor") or "loop"),
        active=bool(data.get("active")),
        activated_at=str(data.get("activated_at")),
        rolled_back_at=data.get("rolled_back_at"),
    )


class ActivationStore:
    """Versioned activation history with rollback."""

    def __init__(
        self,
        path: Path | str = DB_PATH,
        proposals: ProposalStore | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.proposals = proposals or ProposalStore(path=self.path)
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)

    # --------------------------------------------------
    # Activation / rollback
    # --------------------------------------------------

    def activate(
        self, proposal_id: str, *, actor: str = "loop"
    ) -> ActivationRecord:
        """Activate an APPROVED proposal as a new version."""
        proposal = self.proposals.get(proposal_id)
        if proposal is None:
            raise KeyError(f"unknown proposal: {proposal_id}")
        if proposal.status != ProposalStatus.APPROVED:
            raise ValueError(
                "only APPROVED proposals can activate; "
                f"{proposal_id} is {proposal.status.value}"
            )
        actor = (actor or "").strip() or "loop"

        with self._lock, sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT version FROM improvement_activations "
                "WHERE active=1 ORDER BY version DESC LIMIT 1",
            ).fetchone()
            previous = int(current["version"]) if current else None
            top = db.execute(
                "SELECT MAX(version) AS m FROM improvement_activations",
            ).fetchone()
            version = int(top["m"] or 0) + 1
            if previous is not None:
                db.execute(
                    "UPDATE improvement_activations SET active=0 "
                    "WHERE version=?",
                    (previous,),
                )
            db.execute(
                """
                INSERT INTO improvement_activations(
                    version, proposal_id, previous_version, actor,
                    active, activated_at
                ) VALUES(?, ?, ?, ?, 1, ?)
                """,
                (version, proposal_id, previous, actor, now_iso()),
            )
            db.commit()

        self.proposals.transition(
            proposal_id, ProposalStatus.ACTIVATED, actor=actor, via="activation"
        )
        record = self.get_version(version)
        assert record is not None
        return record

    def rollback(
        self, *, actor: str = "loop", reason: str | None = None
    ) -> ActivationRecord:
        """Deactivate the current version, restore the previous one."""
        actor = (actor or "").strip() or "loop"
        with self._lock, sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM improvement_activations "
                "WHERE active=1 ORDER BY version DESC LIMIT 1",
            ).fetchone()
            if current is None:
                db.rollback()
                raise ValueError("no active version to roll back")
            record = _row_to_record(current)
            db.execute(
                "UPDATE improvement_activations SET active=0, "
                "rolled_back_at=? WHERE version=?",
                (now_iso(), record.version),
            )
            restored: ActivationRecord | None = None
            if record.previous_version is not None:
                db.execute(
                    "UPDATE improvement_activations SET active=1 "
                    "WHERE version=?",
                    (record.previous_version,),
                )
                row = db.execute(
                    "SELECT * FROM improvement_activations WHERE version=?",
                    (record.previous_version,),
                ).fetchone()
                restored = _row_to_record(row) if row else None
            db.commit()

        self.proposals.transition(
            record.proposal_id,
            ProposalStatus.ROLLED_BACK,
            actor=actor,
            note=reason,
            via="rollback",
        )
        if restored is not None:
            return restored
        return ActivationRecord(
            version=0,
            proposal_id="",
            previous_version=None,
            actor=actor,
            active=False,
            activated_at=now_iso(),
            rolled_back_at=now_iso(),
        )

    # --------------------------------------------------
    # Reads
    # --------------------------------------------------

    def current(self) -> ActivationRecord | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            row = db.execute(
                "SELECT * FROM improvement_activations "
                "WHERE active=1 ORDER BY version DESC LIMIT 1",
            ).fetchone()
        return _row_to_record(row) if row else None

    def get_version(self, version: int) -> ActivationRecord | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            row = db.execute(
                "SELECT * FROM improvement_activations WHERE version=?",
                (int(version),),
            ).fetchone()
        return _row_to_record(row) if row else None

    def history(self, limit: int = 200) -> list[ActivationRecord]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            rows = db.execute(
                "SELECT * FROM improvement_activations "
                "ORDER BY version ASC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [_row_to_record(row) for row in rows]
