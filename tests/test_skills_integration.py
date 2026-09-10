"""
Integration tests for skill-aware planning.
"""
import pytest
import tempfile
from pathlib import Path
from app.skills.integration import (
    classify_and_select_skill,
    build_skill_context,
    decompose_if_needed,
    format_skill_evidence,
    enhance_coder_prompt,
)
from app.skills.protocols import SkillIntent


def test_classify_and_select_skill_bugfix():
    """Test classification and skill selection for bugfix."""
    goal = "fix the authentication error"
    
    classification, skill = classify_and_select_skill(goal)
    
    assert classification is not None
    assert skill is not None
    assert skill.skill_id == "bugfix"


def test_classify_and_select_skill_feature():
    """Test classification and skill selection for feature."""
    goal = "add export functionality"
    
    classification, skill = classify_and_select_skill(goal)
    
    assert classification is not None
    assert skill is not None
    assert skill.skill_id == "feature"


def test_build_skill_context_with_python_project():
    """Test building skill context for Python project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "pytest.ini").write_text("")
        (worktree / "requirements.txt").write_text("pytest")
        
        goal = "fix the login bug"
        classification, _ = classify_and_select_skill(goal)
        
        context = build_skill_context(goal, worktree, classification)
        
        assert "SKILL GUIDANCE" in context
        assert "BUGFIX" in context or "bugfix" in context.lower()
        assert "PROJECT TYPE" in context
        assert "python" in context.lower()


def test_build_skill_context_unknown_project():
    """Test building skill context for unknown project type."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        
        goal = "refactor the module"
        classification, _ = classify_and_select_skill(goal)
        
        context = build_skill_context(goal, worktree, classification)
        
        # Should have skill guidance even without project adapter
        assert "SKILL GUIDANCE" in context or "REFACTOR" in context


def test_decompose_if_needed_single_step():
    """Test decomposition returns None for single-step goals."""
    goal = "fix the bug"
    classification, _ = classify_and_select_skill(goal)
    
    plan = decompose_if_needed(goal, classification)
    
    # Single intent should not decompose
    assert plan is None


def test_decompose_if_needed_bugfix_test():
    """Test decomposition for bugfix + test."""
    goal = "fix the bug and add test"
    classification, _ = classify_and_select_skill(goal)
    
    # Force secondary skills for decomposition
    classification.secondary_skills = [SkillIntent.TEST]
    
    plan = decompose_if_needed(goal, classification)
    
    # May decompose into multiple steps
    if plan:
        assert len(plan.steps) >= 1


def test_format_skill_evidence_simple():
    """Test formatting skill evidence without plan."""
    goal = "fix authentication"
    classification, _ = classify_and_select_skill(goal)
    
    evidence = format_skill_evidence(classification)
    
    assert evidence["type"] == "skill_classification"
    assert "primary_skill" in evidence
    assert "confidence" in evidence
    assert "reason" in evidence


def test_format_skill_evidence_with_plan():
    """Test formatting skill evidence with decomposition plan."""
    goal = "fix bug and add test"
    classification, _ = classify_and_select_skill(goal)
    classification.secondary_skills = [SkillIntent.TEST]
    
    plan = decompose_if_needed(goal, classification)
    evidence = format_skill_evidence(classification, plan)
    
    assert evidence["type"] == "skill_classification"
    
    if plan:
        assert "plan" in evidence
        assert "steps" in evidence["plan"]


def test_enhance_coder_prompt():
    """Test enhancing coder prompt with skill context."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "pytest.ini").write_text("")
        
        base_prompt = "You are YODAW Coder."
        goal = "fix the authentication bug"
        
        enhanced, evidence = enhance_coder_prompt(
            base_prompt,
            goal,
            worktree,
        )
        
        # Enhanced prompt should include base + skill context
        assert "YODAW Coder" in enhanced
        assert len(enhanced) >= len(base_prompt)
        
        # Evidence should be structured
        assert evidence["type"] == "skill_classification"
        assert "primary_skill" in evidence


def test_enhance_coder_prompt_preserves_base():
    """Test that enhancement doesn't lose original prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        
        base_prompt = "Original instructions here."
        goal = "refactor the code"
        
        enhanced, evidence = enhance_coder_prompt(
            base_prompt,
            goal,
            worktree,
        )
        
        # Base prompt should still be present
        assert "Original instructions here" in enhanced


def test_skill_context_includes_project_commands():
    """Test skill context includes detected project commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "Cargo.toml").write_text("[package]\nname = 'test'")
        
        goal = "fix the bug"
        classification, _ = classify_and_select_skill(goal)
        
        context = build_skill_context(goal, worktree, classification)
        
        # Should detect Rust project
        assert "rust" in context.lower() or "cargo" in context.lower()


def test_integration_flow_end_to_end():
    """Test complete integration flow from goal to enhanced prompt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "package.json").write_text('{"name": "test"}')
        
        base_prompt = "System: You are a coding assistant."
        goal = "add export feature and tests"
        
        # Full flow
        enhanced, evidence = enhance_coder_prompt(
            base_prompt,
            goal,
            worktree,
        )
        
        # Verify all components
        assert "coding assistant" in enhanced
        assert len(enhanced) > len(base_prompt)
        assert evidence["primary_skill"] in ["feature", "mixed"]
        assert "confidence" in evidence
