"""
Tests for skill task classifier.
"""
import pytest
from app.skills.classifier import (
    deterministic_classify,
    classify_task,
)
from app.skills.protocols import SkillIntent


def test_bugfix_classification():
    """Test deterministic bugfix classification."""
    goals = [
        "fix the login error",
        "repair broken authentication",
        "resolve crash in handler",
        "bug in payment processing",
    ]
    
    for goal in goals:
        result = deterministic_classify(goal)
        assert result is not None
        # Primary or secondary should be BUGFIX
        assert (
            result.primary_skill == SkillIntent.BUGFIX
            or SkillIntent.BUGFIX in result.secondary_skills
        )


def test_refactor_classification():
    """Test deterministic refactor classification."""
    goals = [
        "refactor the user service",
        "clean up database layer",
        "restructure auth module",
        "simplify error handling",
    ]
    
    for goal in goals:
        result = deterministic_classify(goal)
        assert result is not None
        assert (
            result.primary_skill == SkillIntent.REFACTOR
            or SkillIntent.REFACTOR in result.secondary_skills
        )


def test_test_classification():
    """Test deterministic test classification."""
    goal = "write test cases for the module"
    result = deterministic_classify(goal)
    
    assert result is not None
    assert (
        result.primary_skill == SkillIntent.TEST
        or SkillIntent.TEST in result.secondary_skills
    )


def test_feature_classification():
    """Test deterministic feature classification."""
    goals = [
        "add export functionality",
        "implement password reset",
        "create new dashboard",
        "build API endpoint for reports",
    ]
    
    for goal in goals:
        result = deterministic_classify(goal)
        assert result is not None
        assert result.primary_skill == SkillIntent.FEATURE
        assert result.confidence >= 0.8


def test_review_classification():
    """Test deterministic review classification."""
    goals = [
        "review security of auth flow",
        "audit database queries",
        "inspect error handling",
    ]
    
    for goal in goals:
        result = deterministic_classify(goal)
        assert result is not None
        # May classify as REVIEW or SECURITY depending on dominant pattern
        assert (
            result.primary_skill in [SkillIntent.REVIEW, SkillIntent.SECURITY]
            or SkillIntent.REVIEW in result.secondary_skills
        )


def test_documentation_classification():
    """Test deterministic documentation classification."""
    goal = "document the module functions"
    result = deterministic_classify(goal)
    
    assert result is not None
    assert (
        result.primary_skill == SkillIntent.DOCUMENTATION
        or SkillIntent.DOCUMENTATION in result.secondary_skills
    )


def test_dependency_classification():
    """Test deterministic dependency classification."""
    goals = [
        "add caching library",
        "install pytest for testing",
        "update package dependencies",
    ]
    
    for goal in goals:
        result = deterministic_classify(goal)
        assert result is not None
        assert (
            result.primary_skill == SkillIntent.DEPENDENCY
            or SkillIntent.DEPENDENCY in result.secondary_skills
        )


def test_mixed_classification():
    """Test mixed intent classification."""
    # Create a goal with multiple strong, roughly equal signals
    goal = "refactor the code and fix bugs and add tests"
    result = deterministic_classify(goal)
    
    assert result is not None
    # Should recognize multiple intents
    assert (
        result.primary_skill == SkillIntent.MIXED
        or len(result.secondary_skills) >= 2
    )


def test_ambiguous_fallback():
    """Test that ambiguous goals return None for LLM fallback."""
    goal = "make it better"
    result = deterministic_classify(goal)
    
    # Deterministic classifier should return None for ambiguous input
    assert result is None


def test_no_match():
    """Test goal with no clear match."""
    goal = "hello world"
    result = deterministic_classify(goal)
    
    assert result is None


def test_classify_task_with_mock_llm():
    """Test full classification with mocked LLM."""
    # Mock provider for LLM fallback
    class MockProvider:
        def chat(self, system_prompt, user_prompt):
            return '{"primary_skill": "feature", "secondary_skills": [], "confidence": 0.8, "reason": "mock"}'
    
    goal = "do something unclear"
    result = classify_task(goal, provider=MockProvider())
    
    assert result.primary_skill == SkillIntent.FEATURE
    assert result.confidence == 0.8
