"""
Tests for Task Decomposition.
"""

import pytest

from app.skills.models import Intent, Skill, SkillId, SkillInput, SkillPrerequisite, PlanningGuidance, ValidationGuidance, RiskLevel, EvidenceSchema, ClassificationResult
from app.skills.registry import SkillRegistry
from app.skills.selection import SkillSelector, SelectionContext
from app.skills.decomposition import TaskDecomposer
from app.skills.skills import BugfixSkill, RefactorSkill, TestSkill, ReviewSkill, FeatureSkill


@pytest.fixture
def registry():
    """Create a registry with test skills."""
    reg = SkillRegistry()
    for skill_class in [BugfixSkill, RefactorSkill, TestSkill, ReviewSkill, FeatureSkill]:
        skill = skill_class().create_skill()
        reg.register(skill)
    return reg


@pytest.fixture
def selector(registry):
    """Create a selector."""
    return SkillSelector(registry)


@pytest.fixture
def decomposer(registry, selector):
    """Create a decomposer."""
    return TaskDecomposer(registry, selector)


class TestTaskDecomposer:
    """Tests for TaskDecomposer."""

    def test_decompose_bugfix(self, decomposer, registry):
        """Test decomposing a bugfix task."""
        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=["test", "review"],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        context = SelectionContext(
            task_description="Fix the login bug",
            classification=classification,
            project_language="python",
        )

        result = decomposer.decompose(context)

        assert len(result.steps) > 0
        assert result.total_estimated_minutes > 0
        assert result.risk_level in [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        assert isinstance(result.requires_human_review, bool)

        # Check step structure
        step_ids = [s.step_id for s in result.steps]
        assert any("reproduce" in sid for sid in step_ids)
        assert any("diagnose" in sid for sid in step_ids)
        assert any("minimal_fix" in sid for sid in step_ids)
        assert any("targeted_tests" in sid for sid in step_ids)
        assert any("broader_tests" in sid for sid in step_ids)
        assert any("diff_review" in sid for sid in step_ids)
        assert any("commit" in sid for sid in step_ids)

    def test_decompose_refactor(self, decomposer, registry):
        """Test decomposing a refactor task."""
        classification = ClassificationResult(
            primary_skill="refactor",
            secondary_skills=["test", "review"],
            confidence=0.85,
            reason="Refactor task",
            detected_intent=Intent.REFACTOR,
        )

        context = SelectionContext(
            task_description="Refactor the user service",
            classification=classification,
            project_language="python",
        )

        result = decomposer.decompose(context)

        assert len(result.steps) > 0
        step_ids = [s.step_id for s in result.steps]
        assert any("analyze" in sid for sid in step_ids)
        assert any("plan" in sid for sid in step_ids)
        assert any("setup_tests" in sid for sid in step_ids)
        assert any("execute" in sid for sid in step_ids)
        assert any("verify" in sid for sid in step_ids)
        assert any("review" in sid for sid in step_ids)
        assert any("commit" in sid for sid in step_ids)

    def test_decompose_test(self, decomposer, registry):
        """Test decomposing a test task."""
        classification = ClassificationResult(
            primary_skill="test",
            secondary_skills=[],
            confidence=0.85,
            reason="Test task",
            detected_intent=Intent.TEST,
        )

        context = SelectionContext(
            task_description="Write tests for user service",
            classification=classification,
            project_language="python",
        )

        result = decomposer.decompose(context)

        assert len(result.steps) > 0
        step_ids = [s.step_id for s in result.steps]
        assert any("analyze" in sid for sid in step_ids)
        assert any("design" in sid for sid in step_ids)
        assert any("implement" in sid for sid in step_ids)
        assert any("run" in sid for sid in step_ids)
        assert any("coverage" in sid for sid in step_ids)
        assert any("commit" in sid for sid in step_ids)

    def test_decompose_feature(self, decomposer, registry):
        """Test decomposing a feature task."""
        classification = ClassificationResult(
            primary_skill="feature",
            secondary_skills=["test", "documentation", "review"],
            confidence=0.8,
            reason="Feature task",
            detected_intent=Intent.FEATURE,
        )

        context = SelectionContext(
            task_description="Implement new API endpoint",
            classification=classification,
            project_language="python",
        )

        result = decomposer.decompose(context)

        assert len(result.steps) > 0
        step_ids = [s.step_id for s in result.steps]
        assert any("requirements" in sid for sid in step_ids)
        assert any("design" in sid for sid in step_ids)
        assert any("implement" in sid for sid in step_ids)
        assert any("test" in sid for sid in step_ids)
        assert any("document" in sid for sid in step_ids)
        assert any("review" in sid for sid in step_ids)
        assert any("commit" in sid for sid in step_ids)

    def test_decompose_with_custom_steps(self, decomposer, registry):
        """Test decomposing with custom step template."""
        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=[],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        context = SelectionContext(
            task_description="Fix bug",
            classification=classification,
        )

        custom_steps = [
            {"name": "custom_step_1", "description": "Custom step 1", "duration": 10, "requires_approval": False},
            {"name": "custom_step_2", "description": "Custom step 2", "duration": 20, "requires_approval": True},
        ]

        result = decomposer.decompose(context, custom_steps=custom_steps)

        assert len(result.steps) == 2
        assert result.steps[0].step_id == "step_1_custom_step_1"
        assert result.steps[1].step_id == "step_2_custom_step_2"
        assert result.steps[0].estimated_duration_minutes == 10
        assert result.steps[1].estimated_duration_minutes == 20
        assert result.steps[1].requires_approval is True

    def test_decompose_step_dependencies(self, decomposer, registry):
        """Test that steps have proper dependencies."""
        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=[],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        context = SelectionContext(
            task_description="Fix bug",
            classification=classification,
        )

        result = decomposer.decompose(context)

        # Each step after the first should depend on the previous
        for i in range(1, len(result.steps)):
            step = result.steps[i]
            assert len(step.depends_on) > 0
            assert step.depends_on[0] == result.steps[i-1].step_id

    def test_decompose_rollback_steps(self, decomposer, registry):
        """Test that approval steps have rollback references."""
        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=[],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        context = SelectionContext(
            task_description="Fix bug",
            classification=classification,
        )

        result = decomposer.decompose(context)

        # Steps that require approval should have rollback_step_id pointing to previous
        for i, step in enumerate(result.steps):
            if step.requires_approval and i > 0:
                assert step.rollback_step_id == result.steps[i-1].step_id

    def test_create_rollback_plan(self, decomposer, registry):
        """Test creating rollback plan."""
        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=[],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        context = SelectionContext(
            task_description="Fix bug",
            classification=classification,
        )

        result = decomposer.decompose(context)
        rollback_plan = decomposer.create_rollback_plan(result)

        assert "decomposition_id" in rollback_plan
        assert "rollback_steps" in rollback_plan
        assert "requires_manual_intervention" in rollback_plan
        assert isinstance(rollback_plan["rollback_steps"], list)

    def test_decompose_all_intents(self, decomposer, registry):
        """Test decomposition works for all intent types."""
        intents = [
            Intent.BUGFIX, Intent.REFACTOR, Intent.TEST, Intent.REVIEW,
            Intent.FEATURE, Intent.DEPENDENCY, Intent.DOCUMENTATION,
            Intent.MIGRATION, Intent.PERFORMANCE, Intent.SECURITY,
        ]

        for intent in intents:
            classification = ClassificationResult(
                primary_skill=intent.value,
                secondary_skills=[],
                confidence=0.8,
                reason=f"{intent.value} task",
                detected_intent=intent,
            )

            context = SelectionContext(
                task_description=f"Do {intent.value}",
                classification=classification,
            )

            result = decomposer.decompose(context)
            assert len(result.steps) > 0, f"No steps for {intent.value}"
            assert result.total_estimated_minutes > 0, f"No estimate for {intent.value}"


class TestDecompositionResult:
    """Tests for DecompositionResult."""

    def test_result_structure(self, decomposer, registry):
        """Test decomposition result has all required fields."""
        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=[],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        context = SelectionContext(
            task_description="Fix bug",
            classification=classification,
        )

        result = decomposer.decompose(context)

        assert hasattr(result, 'steps')
        assert hasattr(result, 'total_estimated_minutes')
        assert hasattr(result, 'requires_human_review')
        assert hasattr(result, 'risk_level')
        assert hasattr(result, 'metadata')

        assert isinstance(result.steps, list)
        assert isinstance(result.total_estimated_minutes, int)
        assert isinstance(result.requires_human_review, bool)
        assert isinstance(result.metadata, dict)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])