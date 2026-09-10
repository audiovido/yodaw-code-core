"""Persistent store for improvement proposals.

SQLite-backed, hermetic-friendly: pass an explicit ``path`` in tests so
no test touches the developer database. Duplicate suppression lives
here — a candidate whose ``content_hash`` matches an existing proposal
is linked to the original instead of creating a new row. Rejected
proposals are retained with reasons; nothing is deleted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.improvement.models import (
    ImprovementProposal,
    ProposalKind,
    ProposalStatus,
    can_transition,
    now_iso,
    proposal_id_for,
)
from app.storage.db import DB_PATH


def _ensure_table(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS improvement_proposals (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            cluster_id TEXT,
            source TEXT NOT NULL DEFAULT 'self-improving-loop',
            status TEXT NOT NULL DEFAULT 'DRAFT',
            expected_impact TEXT NOT NULL DEFAULT '',
            risk_score REAL NOT NULL DEFAULT 0.0,
            validation_plan TEXT NOT NULL DEFAULT '[]',
            content TEXT NOT NULL DEFAULT '{}',
            content_hash TEXT NOT NULL DEFAULT '',
            benchmark TEXT,
            regression TEXT,
            submitted_by TEXT NOT NULL DEFAULT 'loop',
            reviewed_by TEXT,
            review_note TEXT,
            rejection_reason TEXT,
            duplicate_of TEXT,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_imp_status ON improvement_proposals(status)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_imp_hash ON improvement_proposals(content_hash)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_imp_cluster ON improvement_proposals(cluster_id)"
    )


def _row_to_proposal(row: sqlite3.Row) -> ImprovementProposal:
    data = dict(row)
    data["validation_plan"] = json.loads(data.get("validation_plan") or "[]")
    data["content"] = json.loads(data.get("content") or "{}")
    data["benchmark"] = json.loads(data["benchmark"]) if data.get("benchmark") else None
    data["regression"] = (
        json.loads(data["regression"]) if data.get("regression") else None
    )
    return ImprovementProposal.from_dict(data)


class ProposalStore:
    """SQLite-backed proposal store with duplicate suppression."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)

    # --------------------------------------------------
    # Writes
    # --------------------------------------------------

    def create(
        self,
        *,
        kind: str,
        title: str,
        description: str = "",
        cluster_id: str | None = None,
        expected_impact: str = "",
        risk_score: float = 0.0,
        validation_plan: list[str] | None = None,
        content: dict[str, Any] | None = None,
        content_hash: str = "",
        submitted_by: str = "loop",
        source: str = "self-improving-loop",
    ) -> tuple[ImprovementProposal, bool]:
        """Create a proposal, or suppress as a duplicate.

        Returns (proposal, created). When ``content_hash`` matches an
        existing row, no new row is written; the existing proposal is
        returned with ``created=False`` and the caller can record the
        ``duplicate_of`` link.
        """
        content = dict(content or {})
        plan = list(validation_plan or [])
        existing = self.find_by_hash(content_hash) if content_hash else None
        if existing is not None:
            return existing, False

        proposal_id = proposal_id_for(
            kind=str(kind),
            cluster_id=cluster_id or "",
            content_hash=content_hash or title,
        )
        now = now_iso()
        with self._lock, sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM improvement_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
            suffix = 1
            final_id = proposal_id
            while row is not None:
                suffix += 1
                final_id = f"{proposal_id}_{suffix}"
                row = db.execute(
                    "SELECT id FROM improvement_proposals WHERE id=?",
                    (final_id,),
                ).fetchone()
            db.execute(
                """
                INSERT INTO improvement_proposals(
                    id, kind, title, description, cluster_id, source,
                    status, expected_impact, risk_score, validation_plan,
                    content, content_hash, submitted_by, version,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    final_id,
                    str(kind),
                    title,
                    description,
                    cluster_id,
                    source,
                    ProposalStatus.DRAFT.value,
                    expected_impact,
                    float(risk_score),
                    json.dumps(plan),
                    json.dumps(content),
                    content_hash,
                    submitted_by,
                    now,
                    now,
                ),
            )
            db.commit()
        proposal = self.get(final_id)
        assert proposal is not None
        return proposal, True

    def save(self, proposal: ImprovementProposal) -> ImprovementProposal:
        """Persist in-memory mutations (validation evidence, notes)."""
        proposal.touch()
        data = proposal.to_dict()
        with self._lock, sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            db.execute(
                """
                UPDATE improvement_proposals SET
                    title=?, description=?, cluster_id=?, source=?,
                    status=?, expected_impact=?, risk_score=?,
                    validation_plan=?, content=?, content_hash=?,
                    benchmark=?, regression=?, submitted_by=?,
                    reviewed_by=?, review_note=?, rejection_reason=?,
                    duplicate_of=?, version=?, updated_at=?
                WHERE id=?
                """,
                (
                    data["title"],
                    data["description"],
                    data["cluster_id"],
                    data["source"],
                    data["status"],
                    data["expected_impact"],
                    float(data["risk_score"]),
                    json.dumps(data["validation_plan"]),
                    json.dumps(data["content"]),
                    data["content_hash"],
                    json.dumps(data["benchmark"]) if data["benchmark"] else None,
                    json.dumps(data["regression"]) if data["regression"] else None,
                    data["submitted_by"],
                    data["reviewed_by"],
                    data["review_note"],
                    data["rejection_reason"],
                    data["duplicate_of"],
                    int(data["version"]),
                    data["updated_at"],
                    data["id"],
                ),
            )
            db.commit()
        refreshed = self.get(proposal.id)
        assert refreshed is not None
        return refreshed

    def transition(
        self,
        proposal_id: str,
        to: ProposalStatus,
        *,
        actor: str = "loop",
        note: str | None = None,
        reason: str | None = None,
        via: str | None = None,
    ) -> ImprovementProposal:
        """Move a proposal through one explicit state transition.

        ACTIVATED is reachable only via the versioned
        ``ActivationStore.activate`` path (``via="activation"``) and
        ROLLED_BACK only via ``ActivationStore.rollback``
        (``via="rollback"``), so no caller can silently flip a
        proposal into an active state.
        """
        proposal = self.get(proposal_id)
        if proposal is None:
            raise KeyError(f"unknown proposal: {proposal_id}")
        if to == ProposalStatus.ACTIVATED and via != "activation":
            raise ValueError("ACTIVATED requires ActivationStore.activate")
        if to == ProposalStatus.ROLLED_BACK and via != "rollback":
            raise ValueError("ROLLED_BACK requires ActivationStore.rollback")
        frm = proposal.status
        if not can_transition(frm, to):
            raise ValueError(f"illegal transition {frm.value} -> {to.value}")
        proposal.status = to
        proposal.version += 1
        if to == ProposalStatus.PENDING_REVIEW:
            proposal.submitted_by = actor
        if to in (ProposalStatus.APPROVED, ProposalStatus.REJECTED):
            proposal.reviewed_by = actor
            proposal.review_note = note
        if to == ProposalStatus.REJECTED:
            proposal.rejection_reason = reason or note or "rejected"
        return self.save(proposal)

    # --------------------------------------------------
    # Reads
    # --------------------------------------------------

    def get(self, proposal_id: str) -> ImprovementProposal | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            row = db.execute(
                "SELECT * FROM improvement_proposals WHERE id=?",
                (proposal_id,),
            ).fetchone()
        return _row_to_proposal(row) if row is not None else None

    def find_by_hash(self, content_hash: str) -> ImprovementProposal | None:
        if not content_hash:
            return None
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            row = db.execute(
                "SELECT * FROM improvement_proposals "
                "WHERE content_hash=? ORDER BY created_at ASC LIMIT 1",
                (content_hash,),
            ).fetchone()
        return _row_to_proposal(row) if row is not None else None

    def list(
        self,
        *,
        status: ProposalStatus | None = None,
        kind: ProposalKind | str | None = None,
        limit: int = 200,
    ) -> list[ImprovementProposal]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status.value if isinstance(status, ProposalStatus) else str(status))
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind.value if isinstance(kind, ProposalKind) else str(kind))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.extend([max(1, min(int(limit), 1000))])
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            _ensure_table(db)
            rows = db.execute(
                f"SELECT * FROM improvement_proposals {where}"
                "ORDER BY created_at ASC LIMIT ?",
                params,
            ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    def count(self, status: ProposalStatus | None = None) -> int:
        query: str
        params: list[Any]
        if status is None:
            query, params = ("SELECT COUNT(*) FROM improvement_proposals", [])
        else:
            query, params = (
                "SELECT COUNT(*) FROM improvement_proposals WHERE status=?",
                [status.value],
            )
        with sqlite3.connect(self.path) as db:
            _ensure_table(db)
            return int(db.execute(query, params).fetchone()[0])
