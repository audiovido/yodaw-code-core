"""
Skill evidence system for YODAW Coder Skills.

Tracks, stores, and retrieves evidence from skill executions for learning
and audit purposes.
"""

import json
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from .models import SkillEvidence, EvidenceSchema, SkillId


@dataclass
class EvidenceRecord:
    """Stored evidence record."""
    id: str
    skill_id: SkillId
    execution_id: str
    timestamp: str
    success: bool
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceRecord":
        return cls(**data)

    @classmethod
    def from_evidence(cls, evidence: SkillEvidence, tags: Optional[list[str]] = None) -> "EvidenceRecord":
        return cls(
            id=str(uuid4()),
            skill_id=evidence.skill_id,
            execution_id=evidence.execution_id,
            timestamp=evidence.timestamp,
            success=evidence.success,
            inputs=evidence.inputs,
            outputs=evidence.outputs,
            artifacts=evidence.artifacts,
            metrics=evidence.metrics,
            errors=evidence.errors,
            metadata=evidence.metadata,
            tags=tags or [],
        )


class EvidenceStore:
    """Persistent store for skill execution evidence."""

    def __init__(self, db_path: str = "skill_evidence.db"):
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY,
                    skill_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    inputs TEXT NOT NULL,
                    outputs TEXT NOT NULL,
                    artifacts TEXT NOT NULL,
                    metrics TEXT NOT NULL,
                    errors TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    tags TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_skill_id ON evidence(skill_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_execution_id ON evidence(execution_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON evidence(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_success ON evidence(success)
            """)

    def store(self, record: EvidenceRecord) -> str:
        """Store an evidence record."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO evidence (id, skill_id, execution_id, timestamp, success, inputs, outputs, artifacts, metrics, errors, metadata, tags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.id,
                record.skill_id,
                record.execution_id,
                record.timestamp,
                1 if record.success else 0,
                json.dumps(record.inputs),
                json.dumps(record.outputs),
                json.dumps(record.artifacts),
                json.dumps(record.metrics),
                json.dumps(record.errors),
                json.dumps(record.metadata),
                json.dumps(record.tags),
            ))
        return record.id

    def get(self, record_id: str) -> Optional[EvidenceRecord]:
        """Get an evidence record by ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM evidence WHERE id = ?", (record_id,)).fetchone()
            if row:
                return self._row_to_record(row)
        return None

    def get_by_execution(self, execution_id: str) -> list[EvidenceRecord]:
        """Get all evidence for an execution."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM evidence WHERE execution_id = ? ORDER BY timestamp",
                (execution_id,)
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def get_by_skill(self, skill_id: SkillId, limit: int = 100, offset: int = 0) -> list[EvidenceRecord]:
        """Get evidence for a skill."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM evidence WHERE skill_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (skill_id, limit, offset)
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def query(
        self,
        skill_id: Optional[SkillId] = None,
        success: Optional[bool] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        tags: Optional[list[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[EvidenceRecord]:
        """Query evidence with filters."""
        conditions = []
        params = []

        if skill_id:
            conditions.append("skill_id = ?")
            params.append(skill_id)

        if success is not None:
            conditions.append("success = ?")
            params.append(1 if success else 0)

        if since:
            conditions.append("timestamp >= ?")
            params.append(since)

        if until:
            conditions.append("timestamp <= ?")
            params.append(until)

        if tags:
            # Simple tag matching - any tag matches
            tag_conditions = " OR ".join(["tags LIKE ?" for _ in tags])
            conditions.append(f"({tag_conditions})")
            for tag in tags:
                params.append(f"%{tag}%")

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM evidence{where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()
            return [self._row_to_record(row) for row in rows]

    def count(
        self,
        skill_id: Optional[SkillId] = None,
        success: Optional[bool] = None,
    ) -> int:
        """Count evidence records matching criteria."""
        conditions = []
        params = []

        if skill_id:
            conditions.append("skill_id = ?")
            params.append(skill_id)

        if success is not None:
            conditions.append("success = ?")
            params.append(1 if success else 0)

        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM evidence{where_clause}",
                params
            ).fetchone()
            return row[0] if row else 0

    def _row_to_record(self, row: sqlite3.Row) -> EvidenceRecord:
        return EvidenceRecord(
            id=row["id"],
            skill_id=row["skill_id"],
            execution_id=row["execution_id"],
            timestamp=row["timestamp"],
            success=bool(row["success"]),
            inputs=json.loads(row["inputs"]),
            outputs=json.loads(row["outputs"]),
            artifacts=json.loads(row["artifacts"]),
            metrics=json.loads(row["metrics"]),
            errors=json.loads(row["errors"]),
            metadata=json.loads(row["metadata"]),
            tags=json.loads(row["tags"]),
        )

    def delete(self, record_id: str) -> bool:
        """Delete an evidence record."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM evidence WHERE id = ?", (record_id,))
            return cursor.rowcount > 0

    def clear(self):
        """Clear all evidence (use with caution)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM evidence")


class EvidenceValidator:
    """Validates evidence against skill evidence schema."""

    def __init__(self, registry: 'SkillRegistry'):
        self.registry = registry

    def validate(self, evidence: SkillEvidence) -> list[str]:
        """Validate evidence against skill schema."""
        errors = []
        skill = self.registry.get(evidence.skill_id)

        if not skill:
            errors.append(f"Skill {evidence.skill_id} not found in registry")
            return errors

        schema = skill.evidence_schema

        # Check required fields
        for field_name in schema.required_fields:
            if field_name not in evidence.outputs:
                errors.append(f"Required output field missing: {field_name}")

        # Check artifact types
        for artifact_name, artifact_path in evidence.artifacts.items():
            # Could validate file exists, type matches, etc.
            pass

        return errors

    def validate_and_store(self, evidence: SkillEvidence, store: EvidenceStore, tags: Optional[list[str]] = None) -> tuple[bool, list[str]]:
        """Validate and store evidence."""
        errors = self.validate(evidence)
        if errors:
            return False, errors

        record = EvidenceRecord.from_evidence(evidence, tags)
        store.store(record)
        return True, []


class EvidenceAggregator:
    """Aggregates evidence for analytics and learning."""

    def __init__(self, store: EvidenceStore):
        self.store = store

    def get_skill_statistics(self, skill_id: SkillId) -> dict[str, Any]:
        """Get statistics for a skill."""
        total = self.store.count(skill_id=skill_id)
        successful = self.store.count(skill_id=skill_id, success=True)
        failed = self.store.count(skill_id=skill_id, success=False)

        records = self.store.get_by_skill(skill_id, limit=1000)

        # Aggregate metrics
        all_metrics = {}
        for record in records:
            for key, value in record.metrics.items():
                if key not in all_metrics:
                    all_metrics[key] = []
                all_metrics[key].append(value)

        metric_stats = {}
        for key, values in all_metrics.items():
            if values:
                metric_stats[key] = {
                    "count": len(values),
                    "avg": sum(values) / len(values),
                    "min": min(values),
                    "max": max(values),
                }

        return {
            "skill_id": skill_id,
            "total_executions": total,
            "successful": successful,
            "failed": failed,
            "success_rate": successful / total if total > 0 else 0,
            "metric_statistics": metric_stats,
        }

    def get_execution_timeline(self, execution_id: str) -> list[EvidenceRecord]:
        """Get chronological evidence for an execution."""
        return self.store.get_by_execution(execution_id)

    def find_similar_executions(
        self,
        skill_id: SkillId,
        input_signature: dict[str, Any],
        limit: int = 10,
    ) -> list[EvidenceRecord]:
        """Find similar past executions (simple heuristic)."""
        records = self.store.get_by_skill(skill_id, limit=500)

        # Score by input similarity
        scored = []
        for record in records:
            score = self._similarity_score(input_signature, record.inputs)
            scored.append((record, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [r for r, _ in scored[:limit]]

    def _similarity_score(self, inputs1: dict[str, Any], inputs2: dict[str, Any]) -> float:
        """Simple similarity score between input dictionaries."""
        if not inputs1 or not inputs2:
            return 0.0

        keys1 = set(inputs1.keys())
        keys2 = set(inputs2.keys())

        intersection = keys1 & keys2
        union = keys1 | keys2

        if not union:
            return 0.0

        # Jaccard similarity on keys
        key_similarity = len(intersection) / len(union)

        # Value similarity for common keys
        value_similarity = 0.0
        if intersection:
            matches = sum(1 for k in intersection if inputs1[k] == inputs2[k])
            value_similarity = matches / len(intersection)

        return (key_similarity + value_similarity) / 2


def create_evidence_store(db_path: str = "skill_evidence.db") -> EvidenceStore:
    """Factory for evidence store."""
    return EvidenceStore(db_path)


def create_evidence_validator(registry: 'SkillRegistry') -> EvidenceValidator:
    """Factory for evidence validator."""
    return EvidenceValidator(registry)


def create_evidence_aggregator(store: EvidenceStore) -> EvidenceAggregator:
    """Factory for evidence aggregator."""
    return EvidenceAggregator(store)