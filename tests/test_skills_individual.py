"""
Tests for Individual Skills.
"""

import pytest

from app.skills.skills import (
    BugfixSkill, RefactorSkill, TestSkill, ReviewSkill,
    FeatureSkill, DependencySkill, DocumentationSkill,
    MigrationSkill, PerformanceSkill, SecuritySkill,
)
from app.skills.models import Intent, RiskLevel


class TestBugfixSkill:
    """Tests for BugfixSkill."""

    def test_skill_creation(self):
        skill = BugfixSkill().create_skill()
        assert skill.id == "bugfix"
        assert skill.name == "Bug Fix"
        assert Intent.BUGFIX in skill.supported_intents
        assert skill.risk == RiskLevel.MEDIUM

    def test_skill_inputs(self):
        skill = BugfixSkill().create_skill()
        input_names = [i.name for i in skill.inputs]
        assert "bug_description" in input_names
        assert "reproduction_steps" in input_names
        assert "expected_behavior" in input_names
        assert "actual_behavior" in input_names

    def test_skill_prerequisites(self):
        skill = BugfixSkill().create_skill()
        prereq_names = [p.name for p in skill.prerequisites]
        assert "test_framework" in prereq_names
        assert "version_control" in prereq_names

    def test_skill_validation_guidance(self):
        skill = BugfixSkill().create_skill()
        assert len(skill.validation_guidance.success_criteria) > 0
        assert len(skill.validation_guidance.validation_commands) > 0


class TestRefactorSkill:
    """Tests for RefactorSkill."""

    def test_skill_creation(self):
        skill = RefactorSkill().create_skill()
        assert skill.id == "refactor"
        assert skill.name == "Refactor"
        assert Intent.REFACTOR in skill.supported_intents
        assert skill.risk == RiskLevel.MEDIUM

    def test_skill_inputs(self):
        skill = RefactorSkill().create_skill()
        input_names = [i.name for i in skill.inputs]
        assert "target_code" in input_names
        assert "refactoring_goal" in input_names
        assert "preserve_behavior" in input_names

    def test_preserve_behavior_default(self):
        skill = RefactorSkill().create_skill()
        preserve_input = next(i for i in skill.inputs if i.name == "preserve_behavior")
        assert preserve_input.default is True


class TestTestSkill:
    """Tests for TestSkill."""

    def test_skill_creation(self):
        skill = TestSkill().create_skill()
        assert skill.id == "test"
        assert skill.name == "Test Writing"
        assert Intent.TEST in skill.supported_intents
        assert skill.risk == RiskLevel.LOW

    def test_skill_inputs(self):
        skill = TestSkill().create_skill()
        input_names = [i.name for i in skill.inputs]
        assert "target_code" in input_names
        assert "test_types" in input_names
        assert "coverage_target" in input_names


class TestReviewSkill:
    """Tests for ReviewSkill."""

    def test_skill_creation(self):
        skill = ReviewSkill().create_skill()
        assert skill.id == "review"
        assert skill.name == "Code Review"
        assert Intent.REVIEW in skill.supported_intents
        assert skill.risk == RiskLevel.LOW

    def test_read_only_default(self):
        skill = ReviewSkill().create_skill()
        readonly_input = next(i for i in skill.inputs if i.name == "read_only")
        assert readonly_input.default is True


class TestFeatureSkill:
    """Tests for FeatureSkill."""

    def test_skill_creation(self):
        skill = FeatureSkill().create_skill()
        assert skill.id == "feature"
        assert skill.name == "Feature Implementation"
        assert Intent.FEATURE in skill.supported_intents
        assert skill.risk == RiskLevel.MEDIUM


class TestDependencySkill:
    """Tests for DependencySkill."""

    def test_skill_creation(self):
        skill = DependencySkill().create_skill()
        assert skill.id == "dependency"
        assert skill.name == "Dependency Management"
        assert Intent.DEPENDENCY in skill.supported_intents
        assert skill.risk == RiskLevel.MEDIUM

    def test_prefer_reuse_default(self):
        skill = DependencySkill().create_skill()
        reuse_input = next(i for i in skill.inputs if i.name == "prefer_reuse")
        assert reuse_input.default is True


class TestDocumentationSkill:
    """Tests for DocumentationSkill."""

    def test_skill_creation(self):
        skill = DocumentationSkill().create_skill()
        assert skill.id == "documentation"
        assert skill.name == "Documentation"
        assert Intent.DOCUMENTATION in skill.supported_intents
        assert skill.risk == RiskLevel.LOW


class TestMigrationSkill:
    """Tests for MigrationSkill."""

    def test_skill_creation(self):
        skill = MigrationSkill().create_skill()
        assert skill.id == "migration"
        assert skill.name == "Migration"
        assert Intent.MIGRATION in skill.supported_intents
        assert skill.risk == RiskLevel.HIGH


class TestPerformanceSkill:
    """Tests for PerformanceSkill."""

    def test_skill_creation(self):
        skill = PerformanceSkill().create_skill()
        assert skill.id == "performance"
        assert skill.name == "Performance Optimization"
        assert Intent.PERFORMANCE in skill.supported_intents
        assert skill.risk == RiskLevel.MEDIUM


class TestSecuritySkill:
    """Tests for SecuritySkill."""

    def test_skill_creation(self):
        skill = SecuritySkill().create_skill()
        assert skill.id == "security"
        assert skill.name == "Security Hardening"
        assert Intent.SECURITY in skill.supported_intents
        assert skill.risk == RiskLevel.HIGH


class TestAllSkillsLanguageSupport:
    """Test that all skills support required languages."""

    REQUIRED_LANGUAGES = ["python", "typescript", "javascript", "go", "rust", "java", "swift"]

    def test_all_skills_support_required_languages(self):
        skills = [
            BugfixSkill().create_skill(),
            RefactorSkill().create_skill(),
            TestSkill().create_skill(),
            ReviewSkill().create_skill(),
            FeatureSkill().create_skill(),
            DependencySkill().create_skill(),
            DocumentationSkill().create_skill(),
            MigrationSkill().create_skill(),
            PerformanceSkill().create_skill(),
            SecuritySkill().create_skill(),
        ]

        for skill in skills:
            for lang in self.REQUIRED_LANGUAGES:
                assert skill.matches_language(lang), f"{skill.id} doesn't support {lang}"


class TestSkillEvidenceSchema:
    """Tests for skill evidence schemas."""

    def test_all_skills_have_evidence_schema(self):
        skills = [
            BugfixSkill().create_skill(),
            RefactorSkill().create_skill(),
            TestSkill().create_skill(),
            ReviewSkill().create_skill(),
            FeatureSkill().create_skill(),
            DependencySkill().create_skill(),
            DocumentationSkill().create_skill(),
            MigrationSkill().create_skill(),
            PerformanceSkill().create_skill(),
            SecuritySkill().create_skill(),
        ]

        for skill in skills:
            assert skill.evidence_schema is not None
            assert isinstance(skill.evidence_schema.required_fields, list)
            assert isinstance(skill.evidence_schema.optional_fields, list)
            assert isinstance(skill.evidence_schema.artifact_types, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])