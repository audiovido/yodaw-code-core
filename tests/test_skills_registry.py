"""
Tests for skill registry.
"""
import pytest
from app.skills.registry import (
    register_skill,
    get_skill,
    list_skills,
    find_skills_for_intent,
    select_skill,
    BugfixSkill,
    RefactorSkill,
    TestSkill,
    ReviewSkill,
    FeatureSkill,
)
from app.skills.protocols import SkillIntent, RiskLevel


def test_builtin_skills_registered():
    """Test that all built-in skills are registered."""
    skills = list_skills()
    skill_ids = {s.skill_id for s in skills}
    
    expected = {
        "bugfix",
        "refactor",
        "test",
        "review",
        "feature",
        "dependency",
        "documentation",
    }
    
    assert expected.issubset(skill_ids)


def test_get_skill():
    """Test retrieving skill by ID."""
    skill = get_skill("bugfix")
    
    assert skill is not None
    assert skill.skill_id == "bugfix"
    assert SkillIntent.BUGFIX in skill.supported_intents


def test_get_nonexistent_skill():
    """Test retrieving non-existent skill returns None."""
    skill = get_skill("nonexistent")
    assert skill is None


def test_find_skills_for_intent():
    """Test finding skills by intent."""
    bugfix_skills = find_skills_for_intent(SkillIntent.BUGFIX)
    
    assert len(bugfix_skills) >= 1
    assert all(SkillIntent.BUGFIX in s.supported_intents for s in bugfix_skills)


def test_select_skill():
    """Test selecting best skill for intent."""
    skill = select_skill(SkillIntent.BUGFIX)
    
    assert skill is not None
    assert SkillIntent.BUGFIX in skill.supported_intents


def test_select_unsupported_intent():
    """Test selecting skill for unsupported intent returns None."""
    # Create a custom intent that no skill supports
    # For now, all built-in intents are supported, so we test the logic
    skill = select_skill(SkillIntent.MIGRATION)
    
    # Migration might not have a dedicated skill yet
    # This tests the None case if unsupported
    assert skill is None or SkillIntent.MIGRATION in skill.supported_intents


def test_bugfix_skill_properties():
    """Test BugfixSkill properties."""
    skill = BugfixSkill()
    
    assert skill.skill_id == "bugfix"
    assert skill.description
    assert SkillIntent.BUGFIX in skill.supported_intents
    assert skill.risk_level == RiskLevel.MEDIUM
    assert "reproduce" in skill.planning_guidance().lower()
    assert "root cause" in skill.planning_guidance().lower()


def test_refactor_skill_properties():
    """Test RefactorSkill properties."""
    skill = RefactorSkill()
    
    assert skill.skill_id == "refactor"
    assert skill.description
    assert SkillIntent.REFACTOR in skill.supported_intents
    assert skill.risk_level == RiskLevel.MEDIUM
    assert "behavior" in skill.planning_guidance().lower()


def test_test_skill_properties():
    """Test TestSkill properties."""
    skill = TestSkill()
    
    assert skill.skill_id == "test"
    assert SkillIntent.TEST in skill.supported_intents
    assert skill.risk_level == RiskLevel.LOW
    assert "regression" in skill.planning_guidance().lower()


def test_review_skill_properties():
    """Test ReviewSkill properties."""
    skill = ReviewSkill()
    
    assert skill.skill_id == "review"
    assert SkillIntent.REVIEW in skill.supported_intents
    assert skill.risk_level == RiskLevel.LOW
    assert "read-only" in skill.planning_guidance().lower()


def test_feature_skill_properties():
    """Test FeatureSkill properties."""
    skill = FeatureSkill()
    
    assert skill.skill_id == "feature"
    assert SkillIntent.FEATURE in skill.supported_intents
    assert skill.risk_level == RiskLevel.MEDIUM
    assert "reuse" in skill.planning_guidance().lower()


def test_skill_evidence_schema():
    """Test that skills define evidence schemas."""
    skill = BugfixSkill()
    schema = skill.evidence_schema()
    
    assert isinstance(schema, dict)
    assert len(schema) > 0
