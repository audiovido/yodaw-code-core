"""
Tests for Skill Learning System.
"""

import pytest
import tempfile
import os

from app.skills.models import SkillEvidence, SkillId, Intent
from app.skills.registry import SkillRegistry
from app.skills.evidence import EvidenceStore, EvidenceRecord
from app.skills.learning import SkillLearningEngine, SkillAwarePlanner, LearningPattern
from app.skills.skills import BugfixSkill, RefactorSkill, TestSkill
from app.skills.classifier import TaskClassifier
from app.skills.selection import SkillSelector, SelectionContext
from app.skills.decomposition import TaskDecomposer


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture
def registry():
    """Create a registry with test skills."""
    reg = SkillRegistry()
    for skill_class in [BugfixSkill, RefactorSkill, TestSkill]:
        skill = skill_class().create_skill()
        reg.register(skill)
    return reg


@pytest.fixture
def evidence_store(temp_db):
    """Create an evidence store."""
    return EvidenceStore(temp_db)


@pytest.fixture
def learning_engine(registry, evidence_store):
    """Create a learning engine."""
    return SkillLearningEngine(registry, evidence_store)


@pytest.fixture
def planner(registry, learning_engine):
    """Create a skill-aware planner."""
    return SkillAwarePlanner(registry, learning_engine)


class TestSkillLearningEngine:
    """Tests for SkillLearningEngine."""

    def test_learn_from_successful_execution(self, learning_engine, evidence_store, registry):
        """Test learning from successful execution."""
        evidence = SkillEvidence(
            skill_id="bugfix",
            execution_id="exec-1",
            timestamp="2024-01-01T10:00:00Z",
            success=True,
            inputs={"bug_type": "null_pointer", "language": "python", "has_tests": True},
            outputs={"reproduction_case": "test.py", "root_cause": "Null pointer", "fix_description": "Added check", "test_results": "passed"},
            artifacts={"test_file": "test.py", "patch_file": "fix.patch"},
            metrics={"duration_minutes": 45.0},
            errors=[],
            metadata={"strategy": "minimal_fix", "approach": "defensive_programming"},
        )

        patterns = learning_engine.learn_from_execution(evidence)
        assert len(patterns) > 0
        pattern = patterns[0]
        assert pattern.skill_id == "bugfix"
        assert pattern.success_rate == 1.0
        assert pattern.sample_count == 1
        assert "defensive_programming" in pattern.effective_strategies

    def test_learn_from_failed_execution(self, learning_engine, evidence_store, registry):
        """Test learning from failed execution."""
        evidence = SkillEvidence(
            skill_id="bugfix",
            execution_id="exec-2",
            timestamp="2024-01-01T11:00:00Z",
            success=False,
            inputs={"bug_type": "race_condition", "language": "go", "has_tests": False},
            outputs={},
            artifacts={},
            metrics={"duration_minutes": 60.0},
            errors=["Could not reproduce", "Flaky test"],
            metadata={},
        )

        patterns = learning_engine.learn_from_execution(evidence)
        assert len(patterns) > 0
        pattern = patterns[0]
        assert pattern.skill_id == "bugfix"
        assert pattern.success_rate == 0.0
        assert "Could not reproduce" in pattern.common_errors
        assert "Flaky test" in pattern.common_errors

    def test_update_existing_pattern(self, learning_engine, evidence_store, registry):
        """Test updating existing pattern with new execution."""
        # First execution
        evidence1 = SkillEvidence(
            skill_id="bugfix",
            execution_id="exec-1",
            timestamp="2024-01-01T10:00:00Z",
            success=True,
            inputs={"bug_type": "null_pointer", "language": "python"},
            outputs={"reproduction_case": "test.py", "root_cause": "Null", "fix_description": "Fix", "test_results": "passed"},
            artifacts={}, metrics={"duration_minutes": 30.0}, errors=[], metadata={}
        )
        learning_engine.learn_from_execution(evidence1)

        # Second execution with same context
        evidence2 = SkillEvidence(
            skill_id="bugfix",
            execution_id="exec-2",
            timestamp="2024-01-01T11:00:00Z",
            success=True,
            inputs={"bug_type": "null_pointer", "language": "python"},
            outputs={"reproduction_case": "test.py", "root_cause": "Null", "fix_description": "Fix", "test_results": "passed"},
            artifacts={}, metrics={"duration_minutes": 25.0}, errors=[], metadata={}
        )
        patterns = learning_engine.learn_from_execution(evidence2)

        # Should update existing pattern
        assert len(patterns) > 0
        pattern = patterns[0]
        assert pattern.sample_count == 2
        assert pattern.success_rate == 1.0

    def test_skill_performance_profile(self, learning_engine, evidence_store, registry):
        """Test skill performance profile creation."""
        # Store multiple executions
        for i in range(10):
            evidence = SkillEvidence(
                skill_id="bugfix",
                execution_id=f"exec-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=i < 8,  # 80% success
                inputs={"bug_type": "null_pointer", "language": "python"},
                outputs={"reproduction_case": "test.py", "root_cause": "Null", "fix_description": "Fix", "test_results": "passed"},
                artifacts={},
                metrics={"duration_minutes": 30.0 + i * 2},
                errors=[] if i < 8 else ["Timeout"],
                metadata={},
            )
            record = evidence_store.store(EvidenceRecord.from_evidence(evidence))

        profile = learning_engine._update_skill_profile("bugfix")
        assert profile is not None
        assert profile.skill_id == "bugfix"
        assert profile.total_executions == 10
        assert profile.success_rate == 0.8
        assert profile.avg_duration_minutes > 0

    def test_get_recommendations(self, learning_engine, evidence_store, registry):
        """Test getting recommendations for a skill."""
        # Build up some history
        for i in range(5):
            evidence = SkillEvidence(
                skill_id="bugfix",
                execution_id=f"exec-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=True,
                inputs={"language": "python", "has_tests": True},
                outputs={"reproduction_case": "test.py", "root_cause": "Null", "fix_description": "Fix", "test_results": "passed"},
                artifacts={}, metrics={"duration_minutes": 30.0}, errors=[], metadata={}
            )
            evidence_store.store(EvidenceRecord.from_evidence(evidence))

        rec = learning_engine.get_recommendations("bugfix", {"language": "python", "has_tests": True})
        assert rec["should_use"] is True
        assert rec["confidence"] > 0
        assert rec["estimated_duration"] > 0

    def test_recommendations_warn_on_problematic_context(self, learning_engine, evidence_store, registry):
        """Test recommendations warn on problematic contexts."""
        # Add successful executions with has_tests=True
        for i in range(5):
            evidence = SkillEvidence(
                skill_id="bugfix",
                execution_id=f"exec-success-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=True,
                inputs={"language": "python", "has_tests": True},
                outputs={"reproduction_case": "test.py", "root_cause": "Null", "fix_description": "Fix", "test_results": "passed"},
                artifacts={}, metrics={"duration_minutes": 30.0}, errors=[], metadata={}
            )
            evidence_store.store(EvidenceRecord.from_evidence(evidence))

        # Add failed executions with has_tests=False
        for i in range(3):
            evidence = SkillEvidence(
                skill_id="bugfix",
                execution_id=f"exec-fail-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=False,
                inputs={"language": "python", "has_tests": False},
                outputs={}, artifacts={}, metrics={"duration_minutes": 60.0}, errors=["No tests"], metadata={}
            )
            evidence_store.store(EvidenceRecord.from_evidence(evidence))

        # Request recommendations for problematic context
        rec = learning_engine.get_recommendations("bugfix", {"language": "python", "has_tests": False})
        assert len(rec["warnings"]) > 0
        assert rec["confidence"] < 1.0

    def test_skill_ranking_adjustment(self, learning_engine, evidence_store, registry):
        """Test skill ranking adjustment based on learning."""
        # Build history for bugfix
        for i in range(5):
            evidence = SkillEvidence(
                skill_id="bugfix",
                execution_id=f"exec-bugfix-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=True,
                inputs={"language": "python"},
                outputs={"reproduction_case": "test.py", "root_cause": "Null", "fix_description": "Fix", "test_results": "passed"},
                artifacts={}, metrics={"duration_minutes": 30.0}, errors=[], metadata={}
            )
            evidence_store.store(EvidenceRecord.from_evidence(evidence))

        # Build history for refactor (lower success)
        for i in range(5):
            evidence = SkillEvidence(
                skill_id="refactor",
                execution_id=f"exec-refactor-{i}",
                timestamp="2024-01-01T10:00:00Z",
                success=i < 2,  # 40% success
                inputs={"language": "python"},
                outputs={}, artifacts={}, metrics={"duration_minutes": 60.0}, errors=["Failed"], metadata={}
            )
            evidence_store.store(EvidenceRecord.from_evidence(evidence))

        candidates = [("bugfix", 0.8), ("refactor", 0.7)]
        adjusted = learning_engine.get_skill_ranking_adjustment(candidates, {"language": "python"})

        # Bugfix should be ranked higher due to better success rate
        assert adjusted[0][0] == "bugfix"
        assert adjusted[0][1] > adjusted[1][1]


class TestSkillAwarePlanner:
    """Tests for SkillAwarePlanner."""

    def test_create_plan(self, planner, registry):
        """Test creating a skill-aware plan."""
        from app.skills.models import ClassificationResult

        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=["test", "review"],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        plan = planner.create_plan(
            task_description="Fix the login bug",
            classification=classification,
            context={"language": "python", "has_tests": True},
        )

        assert "primary_skill" in plan
        assert plan["primary_skill"]["id"] == "bugfix"
        assert "skill_chain" in plan
        assert len(plan["skill_chain"]) > 0
        assert "execution_strategy" in plan
        assert "risk_assessment" in plan
        assert "checkpoints" in plan

    def test_plan_execution_strategy(self, planner, registry):
        """Test execution strategy determination."""
        from app.skills.models import ClassificationResult

        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=[],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        # High confidence, no warnings -> autonomous
        plan = planner.create_plan(
            task_description="Fix bug",
            classification=classification,
            context={"language": "python"},
        )
        assert plan["execution_strategy"] in ["autonomous", "supervised", "guided"]

    def test_plan_risk_assessment(self, planner, registry):
        """Test risk assessment in plan."""
        from app.skills.models import ClassificationResult

        classification = ClassificationResult(
            primary_skill="refactor",
            secondary_skills=[],
            confidence=0.8,
            reason="Refactor task",
            detected_intent=Intent.REFACTOR,
        )

        plan = planner.create_plan(
            task_description="Refactor to new framework",
            classification=classification,
            context={"language": "python"},
        )

        assert plan["risk_assessment"]["level"] in ["low", "medium", "high", "critical"]
        assert "requires_approval" in plan["risk_assessment"]
        assert "mitigation" in plan["risk_assessment"]


class TestLearningPattern:
    """Tests for LearningPattern."""

    def test_pattern_creation(self):
        """Test creating a learning pattern."""
        pattern = LearningPattern(
            pattern_id="test-pattern",
            skill_id="bugfix",
            intent=Intent.BUGFIX,
            context_signature={"language": "python"},
            success_rate=0.8,
            avg_duration_minutes=30.0,
            common_errors=["Timeout"],
            effective_strategies=["minimal_fix"],
            sample_count=10,
            last_updated="2024-01-01T10:00:00Z",
            confidence=0.7,
        )
        assert pattern.pattern_id == "test-pattern"
        assert pattern.skill_id == "bugfix"
        assert pattern.confidence == 0.7


if __name__ == "__main__":
    pytest.main([__file__, "-v"])