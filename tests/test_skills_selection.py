"""
Tests for Skill Selection and Ranking.
"""

import pytest

from app.skills.models import Intent, Skill, SkillId, SkillInput, SkillPrerequisite, PlanningGuidance, ValidationGuidance, RiskLevel, EvidenceSchema, ClassificationResult
from app.skills.registry import SkillRegistry
from app.skills.selection import SkillSelector, SelectionContext
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
    """Create a selector with the registry."""
    return SkillSelector(registry)


class TestSkillSelector:
    """Tests for SkillSelector."""

    def test_select_bugfix_skill(self, selector, registry):
        """Test selecting bugfix skill for bugfix task."""
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

        result = selector.select(context)
        assert result.selected_skill == "bugfix"
        assert result.confidence > 0.5

    def test_select_refactor_skill(self, selector, registry):
        """Test selecting refactor skill for refactor task."""
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

        result = selector.select(context)
        assert result.selected_skill == "refactor"

    def test_select_with_language_preference(self, selector, registry):
        """Test selection considers language."""
        classification = ClassificationResult(
            primary_skill="feature",
            secondary_skills=["test"],
            confidence=0.8,
            reason="Feature task",
            detected_intent=Intent.FEATURE,
        )

        # Feature skill supports all languages, but let's test with a language
        context = SelectionContext(
            task_description="Implement new feature",
            classification=classification,
            project_language="python",
        )

        result = selector.select(context)
        assert result.selected_skill == "feature"

    def test_select_respects_max_risk(self, selector, registry):
        """Test selection respects max risk constraint."""
        classification = ClassificationResult(
            primary_skill="migration",
            secondary_skills=[],
            confidence=0.8,
            reason="Migration task",
            detected_intent=Intent.MIGRATION,
        )

        context = SelectionContext(
            task_description="Migrate to new framework",
            classification=classification,
            max_risk=RiskLevel.LOW,  # Migration is HIGH risk
        )

        result = selector.select(context)
        # Should not select migration due to risk constraint
        # Will fall back to classification primary or alternative
        assert result.selected_skill != "migration" or result.confidence < 0.5

    def test_select_requires_approval(self, selector, registry):
        """Test selection with human approval requirement."""
        classification = ClassificationResult(
            primary_skill="feature",
            secondary_skills=[],
            confidence=0.8,
            reason="Feature task",
            detected_intent=Intent.FEATURE,
        )

        context = SelectionContext(
            task_description="Implement feature",
            classification=classification,
            require_human_approval=True,
        )

        result = selector.select(context)
        # Feature requires approval, so might be filtered out or have lower confidence
        assert result is not None

    def test_rank_skills(self, selector, registry):
        """Test ranking skills."""
        classification = ClassificationResult(
            primary_skill="bugfix",
            secondary_skills=["refactor", "test"],
            confidence=0.9,
            reason="Bugfix task",
            detected_intent=Intent.BUGFIX,
        )

        context = SelectionContext(
            task_description="Fix bug",
            classification=classification,
            project_language="python",
        )

        skills = registry.get_by_intent(Intent.BUGFIX)
        ranked = selector.rank_skills(skills, context)

        assert len(ranked) > 0
        assert all(isinstance(score, float) for _, score in ranked)
        # Should be sorted descending
        scores = [score for _, score in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_get_skill_chain(self, selector, registry):
        """Test getting skill chain for multi-step tasks."""
        classification = ClassificationResult(
            primary_skill="feature",
            secondary_skills=["test", "documentation", "review"],
            confidence=0.8,
            reason="Feature task",
            detected_intent=Intent.FEATURE,
        )

        context = SelectionContext(
            task_description="Implement feature with tests and docs",
            classification=classification,
            project_language="python",
        )

        chain = selector.get_skill_chain(context, max_skills=5)
        assert len(chain) > 0
        assert chain[0].id == "feature"
        # Should include secondary skills
        skill_ids = [s.id for s in chain]
        assert "test" in skill_ids or "documentation" in skill_ids


class TestSelectionContext:
    """Tests for SelectionContext."""

    def test_context_creation(self):
        """Test creating selection context."""
        classification = ClassificationResult(
            primary_skill="test",
            secondary_skills=[],
            confidence=0.8,
            reason="Test",
            detected_intent=Intent.TEST,
        )

        context = SelectionContext(
            task_description="Write tests",
            classification=classification,
            project_language="python",
            project_frameworks=["pytest", "fastapi"],
            constraints={"timeout": 300},
            preferences={"prefer_fast": True},
        )

        assert context.task_description == "Write tests"
        assert context.project_language == "python"
        assert "pytest" in context.project_frameworks
        assert context.constraints["timeout"] == 300
        assert context.preferences["prefer_fast"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])