"""
Tests for the Task Classifier.
"""

import pytest

from app.skills.models import Intent, Skill, SkillId, SkillInput, SkillPrerequisite, PlanningGuidance, ValidationGuidance, RiskLevel, EvidenceSchema
from app.skills.classifier import TaskClassifier, ClassificationRule
from app.skills.registry import SkillRegistry
from app.skills.skills import BugfixSkill, RefactorSkill, TestSkill, ReviewSkill, FeatureSkill, DependencySkill, DocumentationSkill, MigrationSkill, PerformanceSkill, SecuritySkill


@pytest.fixture
def skill_registry():
    """Create a registry with all default skills."""
    registry = SkillRegistry()
    for skill_class in [
        BugfixSkill, RefactorSkill, TestSkill, ReviewSkill,
        FeatureSkill, DependencySkill, DocumentationSkill,
        MigrationSkill, PerformanceSkill, SecuritySkill,
    ]:
        skill = skill_class().create_skill()
        registry.register(skill)
    return registry


@pytest.fixture
def classifier(skill_registry):
    """Create a classifier with the skill registry."""
    return TaskClassifier(skill_registry._skills)


class TestTaskClassifier:
    """Tests for TaskClassifier."""

    def test_classify_bugfix(self, classifier):
        """Test classification of bugfix tasks."""
        result = classifier.classify("Fix the login bug where users get 500 error on password reset")
        assert result.primary_skill == "bugfix"
        assert result.detected_intent == Intent.BUGFIX
        assert result.confidence > 0.7
        assert "bug" in result.reason.lower() or "fix" in result.reason.lower()

    def test_classify_bugfix_reproduce(self, classifier):
        """Test classification of bug reproduction tasks."""
        result = classifier.classify("Reproduce the issue where API returns null for valid input")
        assert result.primary_skill == "bugfix"
        assert result.detected_intent == Intent.BUGFIX
        assert result.confidence > 0.8

    def test_classify_refactor(self, classifier):
        """Test classification of refactoring tasks."""
        result = classifier.classify("Refactor the user service to reduce complexity and remove code duplication")
        assert result.primary_skill == "refactor"
        assert result.detected_intent == Intent.REFACTOR
        assert result.confidence > 0.7

    def test_classify_refactor_technical_debt(self, classifier):
        """Test classification of technical debt tasks."""
        result = classifier.classify("Address technical debt in the payment module - high coupling and low cohesion")
        assert result.primary_skill == "refactor"
        assert result.detected_intent == Intent.REFACTOR

    def test_classify_test(self, classifier):
        """Test classification of test writing tasks."""
        result = classifier.classify("Write unit tests for the authentication module to improve coverage")
        assert result.primary_skill == "test"
        assert result.detected_intent == Intent.TEST
        assert result.confidence > 0.7

    def test_classify_test_coverage(self, classifier):
        """Test classification of coverage improvement tasks."""
        result = classifier.classify("Add missing test coverage for the edge cases in order processing")
        assert result.primary_skill == "test"
        assert result.detected_intent == Intent.TEST

    def test_classify_review(self, classifier):
        """Test classification of review tasks."""
        result = classifier.classify("Review the new API endpoints for security vulnerabilities")
        assert result.primary_skill == "review"
        assert result.detected_intent == Intent.REVIEW
        assert result.confidence > 0.7

    def test_classify_review_readonly(self, classifier):
        """Test classification of read-only review tasks."""
        result = classifier.classify("Perform a read-only code review of the pull request")
        assert result.primary_skill == "review"
        assert result.detected_intent == Intent.REVIEW

    def test_classify_feature(self, classifier):
        """Test classification of feature implementation tasks."""
        result = classifier.classify("Implement a new REST API endpoint for user profile management")
        assert result.primary_skill == "feature"
        assert result.detected_intent == Intent.FEATURE
        assert result.confidence > 0.6

    def test_classify_feature_create(self, classifier):
        """Test classification of feature creation tasks."""
        result = classifier.classify("Create a new dashboard component for analytics visualization")
        assert result.primary_skill == "feature"
        assert result.detected_intent == Intent.FEATURE

    def test_classify_dependency(self, classifier):
        """Test classification of dependency tasks."""
        result = classifier.classify("Upgrade the vulnerable dependency lodash to latest version")
        assert result.primary_skill == "dependency"
        assert result.detected_intent == Intent.DEPENDENCY
        assert result.confidence > 0.7

    def test_classify_dependency_reuse(self, classifier):
        """Test classification of reuse tasks."""
        result = classifier.classify("Reuse the existing shared utility library instead of creating new helpers")
        assert result.primary_skill == "dependency"
        assert result.detected_intent == Intent.DEPENDENCY

    def test_classify_documentation(self, classifier):
        """Test classification of documentation tasks."""
        result = classifier.classify("Update the API documentation with new endpoint examples")
        assert result.primary_skill == "documentation"
        assert result.detected_intent == Intent.DOCUMENTATION
        assert result.confidence > 0.7

    def test_classify_migration(self, classifier):
        """Test classification of migration tasks."""
        result = classifier.classify("Migrate the legacy Python 2 codebase to Python 3")
        assert result.primary_skill == "migration"
        assert result.detected_intent == Intent.MIGRATION
        assert result.confidence > 0.7

    def test_classify_performance(self, classifier):
        """Test classification of performance tasks."""
        result = classifier.classify("Optimize the database queries to reduce latency and improve throughput")
        assert result.primary_skill == "performance"
        assert result.detected_intent == Intent.PERFORMANCE
        assert result.confidence > 0.7

    def test_classify_security(self, classifier):
        """Test classification of security tasks."""
        result = classifier.classify("Fix the SQL injection vulnerability in the search endpoint")
        assert result.primary_skill == "security"
        assert result.detected_intent == Intent.SECURITY
        assert result.confidence > 0.8

    def test_classify_with_language_context(self, classifier):
        """Test classification with language context."""
        result = classifier.classify(
            "Fix the bug in the user service",
            context={"language": "python", "file_paths": ["app/services/user.py"]}
        )
        assert result.primary_skill == "bugfix"
        assert result.detected_language == "python"

    def test_classify_detects_language(self, classifier):
        """Test language detection from task description."""
        result = classifier.classify("Fix the bug in the async function using await and async def")
        assert result.detected_language == "python"

        result = classifier.classify("Fix the bug in the async function using async/await and const")
        assert result.detected_language in ["typescript", "javascript"]

        result = classifier.classify("Fix the bug in the func main using go mod")
        assert result.detected_language == "go"

    def test_classify_secondary_skills(self, classifier):
        """Test that secondary skills are populated."""
        result = classifier.classify("Fix the bug and write tests for the fix")
        assert result.primary_skill == "bugfix"
        assert "test" in result.secondary_skills

    def test_classify_low_confidence_fallback(self, classifier):
        """Test fallback for unclassifiable tasks."""
        result = classifier.classify("Do something completely unrelated to software engineering xyz123")
        # Should still return a result with primary skill
        assert result.primary_skill is not None
        assert result.confidence >= 0.0

    def test_custom_rules(self, classifier):
        """Test adding custom classification rules."""
        custom_rule = ClassificationRule(
            pattern=r"custom.*pattern",
            intent=Intent.FEATURE,
            primary_skill="feature",
            confidence=0.9,
            keywords=["custom"],
        )
        classifier.add_rule(custom_rule)

        result = classifier.classify("This is a custom pattern task")
        assert result.primary_skill == "feature"
        assert result.confidence > 0.8

    def test_classification_result_structure(self, classifier):
        """Test classification result has all required fields."""
        result = classifier.classify("Fix the bug in login")

        assert hasattr(result, 'primary_skill')
        assert hasattr(result, 'secondary_skills')
        assert hasattr(result, 'confidence')
        assert hasattr(result, 'reason')
        assert hasattr(result, 'detected_intent')
        assert hasattr(result, 'detected_language')
        assert hasattr(result, 'metadata')

        assert isinstance(result.secondary_skills, list)
        assert isinstance(result.confidence, float)
        assert 0 <= result.confidence <= 1
        assert isinstance(result.metadata, dict)


class TestClassificationRule:
    """Tests for ClassificationRule."""

    def test_rule_compilation(self):
        """Test that patterns are compiled."""
        rule = ClassificationRule(
            pattern=r"test.*pattern",
            intent=Intent.TEST,
            primary_skill="test",
        )
        assert hasattr(rule, 'compiled_pattern')
        assert rule.compiled_pattern is not None

    def test_rule_matching(self):
        """Test rule pattern matching."""
        rule = ClassificationRule(
            pattern=r"fix.*bug",
            intent=Intent.BUGFIX,
            primary_skill="bugfix",
        )
        assert rule.compiled_pattern.search("fix the bug") is not None
        assert rule.compiled_pattern.search("fix a bug") is not None
        assert rule.compiled_pattern.search("bug fix") is None  # Different order


if __name__ == "__main__":
    pytest.main([__file__, "-v"])