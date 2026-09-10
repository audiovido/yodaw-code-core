"""
Tests for task decomposition.
"""
import pytest
from app.skills.decomposition import (
    has_cycle,
    topological_sort,
    decompose_simple,
    decompose_bugfix_with_test,
    decompose_feature_with_test,
    decompose_task,
    validate_plan,
)
from app.skills.protocols import SkillStep, SkillPlan, SkillIntent


def test_has_cycle_no_cycle():
    """Test cycle detection with no cycle."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=[]),
        SkillStep(step_id="b", description="B", dependencies=["a"]),
        SkillStep(step_id="c", description="C", dependencies=["b"]),
    ]
    
    assert has_cycle(steps) is False


def test_has_cycle_simple_cycle():
    """Test cycle detection with simple cycle."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=["b"]),
        SkillStep(step_id="b", description="B", dependencies=["a"]),
    ]
    
    assert has_cycle(steps) is True


def test_has_cycle_complex_cycle():
    """Test cycle detection with complex cycle."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=["b"]),
        SkillStep(step_id="b", description="B", dependencies=["c"]),
        SkillStep(step_id="c", description="C", dependencies=["a"]),
    ]
    
    assert has_cycle(steps) is True


def test_topological_sort_linear():
    """Test topological sort with linear dependencies."""
    steps = [
        SkillStep(step_id="c", description="C", dependencies=["b"]),
        SkillStep(step_id="a", description="A", dependencies=[]),
        SkillStep(step_id="b", description="B", dependencies=["a"]),
    ]
    
    sorted_steps = topological_sort(steps)
    
    assert sorted_steps is not None
    assert len(sorted_steps) == 3
    assert sorted_steps[0].step_id == "a"
    assert sorted_steps[1].step_id == "b"
    assert sorted_steps[2].step_id == "c"


def test_topological_sort_parallel():
    """Test topological sort with parallel steps."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=[]),
        SkillStep(step_id="b", description="B", dependencies=[]),
        SkillStep(step_id="c", description="C", dependencies=["a", "b"]),
    ]
    
    sorted_steps = topological_sort(steps)
    
    assert sorted_steps is not None
    assert len(sorted_steps) == 3
    # a and b can be in any order, but c must be last
    assert sorted_steps[2].step_id == "c"


def test_topological_sort_with_cycle():
    """Test topological sort returns None for cycle."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=["b"]),
        SkillStep(step_id="b", description="B", dependencies=["a"]),
    ]
    
    sorted_steps = topological_sort(steps)
    assert sorted_steps is None


def test_decompose_simple():
    """Test simple single-step decomposition."""
    goal = "fix authentication bug"
    plan = decompose_simple(goal, SkillIntent.BUGFIX)
    
    assert len(plan.steps) == 1
    assert plan.steps[0].step_id == "main"
    assert plan.steps[0].skill_hint == SkillIntent.BUGFIX
    assert plan.bounded is True
    assert plan.max_steps == 1


def test_decompose_bugfix_with_test():
    """Test bugfix + test decomposition."""
    goal = "fix login error and add test"
    plan = decompose_bugfix_with_test(goal)
    
    assert len(plan.steps) == 2
    assert plan.steps[0].step_id == "bugfix"
    assert plan.steps[0].skill_hint == SkillIntent.BUGFIX
    assert plan.steps[1].step_id == "regression_test"
    assert plan.steps[1].skill_hint == SkillIntent.TEST
    assert "bugfix" in plan.steps[1].dependencies
    assert plan.bounded is True


def test_decompose_feature_with_test():
    """Test feature + test decomposition."""
    goal = "add export and tests"
    plan = decompose_feature_with_test(goal)
    
    assert len(plan.steps) == 2
    assert plan.steps[0].step_id == "feature"
    assert plan.steps[0].skill_hint == SkillIntent.FEATURE
    assert plan.steps[1].step_id == "tests"
    assert plan.steps[1].skill_hint == SkillIntent.TEST
    assert "feature" in plan.steps[1].dependencies


def test_decompose_task_single_skill():
    """Test task decomposition with single skill."""
    goal = "refactor user service"
    plan = decompose_task(goal, SkillIntent.REFACTOR, [])
    
    assert len(plan.steps) == 1
    assert plan.steps[0].skill_hint == SkillIntent.REFACTOR


def test_decompose_task_bugfix_test():
    """Test task decomposition for bugfix + test."""
    goal = "fix bug and add test"
    plan = decompose_task(goal, SkillIntent.BUGFIX, [SkillIntent.TEST])
    
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_hint == SkillIntent.BUGFIX
    assert plan.steps[1].skill_hint == SkillIntent.TEST


def test_decompose_task_feature_test():
    """Test task decomposition for feature + test."""
    goal = "add feature and test"
    plan = decompose_task(goal, SkillIntent.FEATURE, [SkillIntent.TEST])
    
    assert len(plan.steps) == 2
    assert plan.steps[0].skill_hint == SkillIntent.FEATURE
    assert plan.steps[1].skill_hint == SkillIntent.TEST


def test_decompose_task_mixed():
    """Test task decomposition for mixed intent."""
    goal = "refactor and optimize"
    plan = decompose_task(goal, SkillIntent.MIXED, [SkillIntent.REFACTOR, SkillIntent.PERFORMANCE])
    
    # Mixed intent defaults to single step for now
    assert len(plan.steps) == 1


def test_validate_plan_valid():
    """Test plan validation with valid plan."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=[]),
        SkillStep(step_id="b", description="B", dependencies=["a"]),
    ]
    plan = SkillPlan(steps=steps, bounded=True, max_steps=10)
    
    valid, error = validate_plan(plan)
    assert valid is True
    assert error == ""


def test_validate_plan_empty():
    """Test plan validation with empty plan."""
    plan = SkillPlan(steps=[], bounded=True, max_steps=10)
    
    valid, error = validate_plan(plan)
    assert valid is False
    assert "no steps" in error.lower()


def test_validate_plan_exceeds_max():
    """Test plan validation when exceeding max_steps."""
    steps = [
        SkillStep(step_id=f"step_{i}", description=f"Step {i}", dependencies=[])
        for i in range(15)
    ]
    plan = SkillPlan(steps=steps, bounded=True, max_steps=10)
    
    valid, error = validate_plan(plan)
    assert valid is False
    assert "max_steps" in error.lower()


def test_validate_plan_unknown_dependency():
    """Test plan validation with unknown dependency."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=["nonexistent"]),
    ]
    plan = SkillPlan(steps=steps, bounded=True, max_steps=10)
    
    valid, error = validate_plan(plan)
    assert valid is False
    assert "unknown" in error.lower()


def test_validate_plan_with_cycle():
    """Test plan validation with cycle."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=["b"]),
        SkillStep(step_id="b", description="B", dependencies=["a"]),
    ]
    plan = SkillPlan(steps=steps, bounded=True, max_steps=10)
    
    valid, error = validate_plan(plan)
    assert valid is False
    assert "cycle" in error.lower()


def test_parallel_safe_metadata():
    """Test parallel_safe metadata is preserved."""
    steps = [
        SkillStep(step_id="a", description="A", dependencies=[], parallel_safe=True),
        SkillStep(step_id="b", description="B", dependencies=[], parallel_safe=True),
    ]
    plan = SkillPlan(steps=steps)
    
    assert plan.steps[0].parallel_safe is True
    assert plan.steps[1].parallel_safe is True
