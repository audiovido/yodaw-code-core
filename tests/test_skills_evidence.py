"""
Tests for Skill Evidence System.
"""

import pytest
import tempfile
import os

from app.skills.models import SkillEvidence, EvidenceSchema, SkillId
from app.skills.evidence import EvidenceStore, EvidenceRecord, EvidenceValidator, EvidenceAggregator
from app.skills.registry import SkillRegistry
from app.skills.skills import BugfixSkill


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def evidence_store(temp_db):
    """Create an evidence store with temp database."""
    return EvidenceStore(temp_db)


@pytest.fixture
def registry():
    """Create a registry with bugfix skill."""
    reg = SkillRegistry()
    skill = BugfixSkill().create_skill()
    reg.register(skill)
    return reg


@pytest.fixture
def validator(registry):
    """Create an evidence validator."""
    return EvidenceValidator(registry)


@pytest.fixture
def aggregator(evidence_store):
    """Create an evidence aggregator."""
    return EvidenceAggregator(evidence_store)


@pytest.fixture
def sample_evidence():
    """Create sample evidence."""
    return SkillEvidence(
        skill_id="bugfix",
        execution_id="exec-123",
        timestamp="2024-01-01T10:00:00Z",
        success=True,
        inputs={"bug_description": "Login fails", "reproduction_steps": ["step1", "step2"]},
        outputs={"reproduction_case": "test_login.py", "root_cause": "Null pointer", "fix_description": "Added null check", "test_results": "passed"},
        artifacts={"test_file": "test_login.py", "patch_file": "fix.patch"},
        metrics={"duration_minutes": 45.0, "tests_passed": 10, "tests_failed": 0},
        errors=[],
        metadata={"environment": "dev"},
    )


class TestEvidenceStore:
    """Tests for EvidenceStore."""

    def test_store_and_retrieve(self, evidence_store, sample_evidence):
        """Test storing and retrieving evidence."""
        record = EvidenceRecord.from_evidence(sample_evidence, tags=["test"])
        stored_id = evidence_store.store(record)

        assert stored_id == record.id

        retrieved = evidence_store.get(stored_id)
        assert retrieved is not None
        assert retrieved.skill_id == "bugfix"
        assert retrieved.execution_id == "exec-123"
        assert retrieved.success is True
        assert retrieved.inputs["bug_description"] == "Login fails"
        assert retrieved.outputs["root_cause"] == "Null pointer"
        assert "test_file" in retrieved.artifacts
        assert retrieved.metrics["duration_minutes"] == 45.0

    def test_get_by_execution(self, evidence_store, sample_evidence):
        """Test getting evidence by execution ID."""
        record1 = EvidenceRecord.from_evidence(sample_evidence, tags=["step1"])
        record2 = EvidenceRecord.from_evidence(
            SkillEvidence(
                skill_id="test",
                execution_id="exec-123",
                timestamp="2024-01-01T10:30:00Z",
                success=True,
                inputs={}, outputs={}, artifacts={}, metrics={}, errors=[], metadata={}
            ),
            tags=["step2"]
        )
        evidence_store.store(record1)
        evidence_store.store(record2)

        records = evidence_store.get_by_execution("exec-123")
        assert len(records) == 2
        assert records[0].skill_id == "bugfix"
        assert records[1].skill_id == "test"

    def test_get_by_skill(self, evidence_store, sample_evidence):
        """Test getting evidence by skill ID."""
        record = EvidenceRecord.from_evidence(sample_evidence)
        evidence_store.store(record)

        records = evidence_store.get_by_skill("bugfix")
        assert len(records) == 1
        assert records[0].skill_id == "bugfix"

    def test_query_filters(self, evidence_store, sample_evidence):
        """Test querying with filters."""
        # Store successful evidence
        record1 = EvidenceRecord.from_evidence(sample_evidence)
        evidence_store.store(record1)

        # Store failed evidence
        failed_evidence = SkillEvidence(
            skill_id="bugfix",
            execution_id="exec-456",
            timestamp="2024-01-01T11:00:00Z",
            success=False,
            inputs={}, outputs={}, artifacts={}, metrics={}, errors=["Fix failed"], metadata={}
        )
        record2 = EvidenceRecord.from_evidence(failed_evidence)
        evidence_store.store(record2)

        # Query successful only
        success_records = evidence_store.query(skill_id="bugfix", success=True)
        assert len(success_records) == 1
        assert success_records[0].success is True

        # Query failed only
        failed_records = evidence_store.query(skill_id="bugfix", success=False)
        assert len(failed_records) == 1
        assert failed_records[0].success is False

    def test_count(self, evidence_store, sample_evidence):
        """Test counting evidence."""
        assert evidence_store.count(skill_id="bugfix") == 0

        record = EvidenceRecord.from_evidence(sample_evidence)
        evidence_store.store(record)

        assert evidence_store.count(skill_id="bugfix") == 1
        assert evidence_store.count(skill_id="bugfix", success=True) == 1
        assert evidence_store.count(skill_id="bugfix", success=False) == 0

    def test_delete(self, evidence_store, sample_evidence):
        """Test deleting evidence."""
        record = EvidenceRecord.from_evidence(sample_evidence)
        evidence_store.store(record)

        assert evidence_store.delete(record.id) is True
        assert evidence_store.get(record.id) is None
        assert evidence_store.delete(record.id) is False  # Already deleted


class TestEvidenceValidator:
    """Tests for EvidenceValidator."""

    def test_validate_success(self, validator, sample_evidence):
        """Test validating valid evidence."""
        errors = validator.validate(sample_evidence)
        assert len(errors) == 0

    def test_validate_missing_required_fields(self, validator):
        """Test validation fails for missing required fields."""
        evidence = SkillEvidence(
            skill_id="bugfix",
            execution_id="exec-123",
            timestamp="2024-01-01T10:00:00Z",
            success=True,
            inputs={},
            outputs={},  # Missing required fields
            artifacts={},
            metrics={},
            errors=[],
            metadata={},
        )
        errors = validator.validate(evidence)
        assert len(errors) > 0
        assert any("reproduction_case" in e for e in errors)

    def test_validate_unknown_skill(self, validator):
        """Test validation fails for unknown skill."""
        evidence = SkillEvidence(
            skill_id="unknown-skill",
            execution_id="exec-123",
            timestamp="2024-01-01T10:00:00Z",
            success=True,
            inputs={}, outputs={}, artifacts={}, metrics={}, errors=[], metadata={}
        )
        errors = validator.validate(evidence)
        assert len(errors) > 0
        assert any("not found" in e for e in errors)

    def test_validate_and_store(self, validator, evidence_store, sample_evidence):
        """Test validate and store."""
        success, errors = validator.validate_and_store(sample_evidence, evidence_store, tags=["test"])
        assert success is True
        assert len(errors) == 0

        # Verify stored
        records = evidence_store.get_by_skill("bugfix")
        assert len(records) == 1
        assert "test" in records[0].tags


class TestEvidenceAggregator:
    """Tests for EvidenceAggregator."""

    def test_get_skill_statistics(self, aggregator, evidence_store, sample_evidence):
        """Test getting skill statistics."""
        # Store multiple executions
        for i in range(5):
            evidence = SkillEvidence(
                skill_id="bugfix",
                execution_id=f"exec-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=i < 4,  # 4 success, 1 failure
                inputs={},
                outputs={"reproduction_case": "test.py", "root_cause": "cause", "fix_description": "fix", "test_results": "passed"},
                artifacts={},
                metrics={"duration_minutes": 30.0 + i * 5},
                errors=[] if i < 4 else ["Failed"],
                metadata={},
            )
            record = EvidenceRecord.from_evidence(evidence)
            evidence_store.store(record)

        stats = aggregator.get_skill_statistics("bugfix")
        assert stats["skill_id"] == "bugfix"
        assert stats["total_executions"] == 5
        assert stats["successful"] == 4
        assert stats["failed"] == 1
        assert stats["success_rate"] == 0.8
        assert "duration_minutes" in stats["metric_statistics"]

    def test_get_execution_timeline(self, aggregator, evidence_store, sample_evidence):
        """Test getting execution timeline."""
        record1 = EvidenceRecord.from_evidence(sample_evidence)
        evidence2 = SkillEvidence(
            skill_id="test",
            execution_id="exec-123",
            timestamp="2024-01-01T10:30:00Z",
            success=True, inputs={}, outputs={}, artifacts={}, metrics={}, errors=[], metadata={}
        )
        record2 = EvidenceRecord.from_evidence(evidence2)
        evidence_store.store(record1)
        evidence_store.store(record2)

        timeline = aggregator.get_execution_timeline("exec-123")
        assert len(timeline) == 2
        assert timeline[0].skill_id == "bugfix"
        assert timeline[1].skill_id == "test"

    def test_find_similar_executions(self, aggregator, evidence_store):
        """Test finding similar executions."""
        # Store executions with similar inputs
        for i in range(3):
            evidence = SkillEvidence(
                skill_id="bugfix",
                execution_id=f"exec-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=True,
                inputs={"bug_type": "null_pointer", "language": "python"},
                outputs={"reproduction_case": "test.py", "root_cause": "cause", "fix_description": "fix", "test_results": "passed"},
                artifacts={}, metrics={}, errors=[], metadata={}
            )
            record = EvidenceRecord.from_evidence(evidence)
            evidence_store.store(record)

        # Store different execution
        evidence = SkillEvidence(
            skill_id="bugfix",
            execution_id="exec-diff",
            timestamp="2024-01-01T10:00:00Z",
            success=True,
            inputs={"bug_type": "memory_leak", "language": "java"},
            outputs={"reproduction_case": "test.py", "root_cause": "cause", "fix_description": "fix", "test_results": "passed"},
            artifacts={}, metrics={}, errors=[], metadata={}
        )
        record = EvidenceRecord.from_evidence(evidence)
        evidence_store.store(record)

        similar = aggregator.find_similar_executions(
            "bugfix",
            {"bug_type": "null_pointer", "language": "python"},
            limit=5
        )
        assert len(similar) >= 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])