"""
Task decomposition: break complex goals into safe, ordered steps.
"""
from typing import Optional, Tuple
from app.skills.protocols import SkillStep, SkillPlan, SkillIntent


def has_cycle(steps: list) -> bool:
    """
    Detect dependency cycles using topological sort approach.
    Returns True if a cycle exists.
    """
    # Build adjacency list
    graph = {step.step_id: step.dependencies for step in steps}
    visited = set()
    rec_stack = set()
    
    def visit(node: str) -> bool:
        if node in rec_stack:
            return True  # Cycle detected
        
        if node in visited:
            return False
        
        visited.add(node)
        rec_stack.add(node)
        
        for neighbor in graph.get(node, []):
            if visit(neighbor):
                return True
        
        rec_stack.remove(node)
        return False
    
    for step in steps:
        if visit(step.step_id):
            return True
    
    return False


def topological_sort(steps: list) -> Optional[list]:
    """
    Return steps in dependency order, or None if cycle detected.
    """
    if has_cycle(steps):
        return None
    
    step_map = {step.step_id: step for step in steps}
    in_degree = {step.step_id: 0 for step in steps}
    
    for step in steps:
        for dep in step.dependencies:
            if dep in in_degree:
                in_degree[step.step_id] += 1
    
    queue = [step_id for step_id, degree in in_degree.items() if degree == 0]
    result = []
    
    while queue:
        step_id = queue.pop(0)
        result.append(step_map[step_id])
        
        for step in steps:
            if step_id in step.dependencies:
                in_degree[step.step_id] -= 1
                if in_degree[step.step_id] == 0:
                    queue.append(step.step_id)
    
    return result if len(result) == len(steps) else None


def decompose_simple(goal: str, primary_intent: SkillIntent) -> SkillPlan:
    """
    Simple decomposition for single-skill goals.
    Most goals should map to a single skill execution.
    """
    steps = [
        SkillStep(
            step_id="main",
            description=goal,
            skill_hint=primary_intent,
            dependencies=[],
            parallel_safe=False,
            validation_required=True,
        )
    ]
    
    return SkillPlan(steps=steps, bounded=True, max_steps=1)


def decompose_bugfix_with_test(goal: str) -> SkillPlan:
    """
    Decompose "fix bug and add test" into ordered steps.
    """
    steps = [
        SkillStep(
            step_id="bugfix",
            description=f"Fix the bug: {goal}",
            skill_hint=SkillIntent.BUGFIX,
            dependencies=[],
            parallel_safe=False,
            validation_required=True,
        ),
        SkillStep(
            step_id="regression_test",
            description="Add regression test for the bug",
            skill_hint=SkillIntent.TEST,
            dependencies=["bugfix"],
            parallel_safe=False,
            validation_required=True,
        ),
    ]
    
    return SkillPlan(steps=steps, bounded=True, max_steps=2)


def decompose_feature_with_test(goal: str) -> SkillPlan:
    """
    Decompose "add feature and test" into ordered steps.
    """
    steps = [
        SkillStep(
            step_id="feature",
            description=f"Implement feature: {goal}",
            skill_hint=SkillIntent.FEATURE,
            dependencies=[],
            parallel_safe=False,
            validation_required=True,
        ),
        SkillStep(
            step_id="tests",
            description="Add tests for the new feature",
            skill_hint=SkillIntent.TEST,
            dependencies=["feature"],
            parallel_safe=False,
            validation_required=True,
        ),
    ]
    
    return SkillPlan(steps=steps, bounded=True, max_steps=2)


def decompose_task(
    goal: str,
    primary_intent: SkillIntent,
    secondary_intents: list,
) -> SkillPlan:
    """
    Decompose a task into steps based on intent classification.
    
    For now, supports bounded decomposition for common patterns:
    - Single skill: one step
    - Bugfix + test: two sequential steps
    - Feature + test: two sequential steps
    
    Future: LLM-driven decomposition for complex goals.
    """
    # Single skill case
    if not secondary_intents:
        return decompose_simple(goal, primary_intent)
    
    # Bugfix + test
    if (
        primary_intent == SkillIntent.BUGFIX
        and SkillIntent.TEST in secondary_intents
    ):
        return decompose_bugfix_with_test(goal)
    
    # Feature + test
    if (
        primary_intent == SkillIntent.FEATURE
        and SkillIntent.TEST in secondary_intents
    ):
        return decompose_feature_with_test(goal)
    
    # Mixed intent: treat as single complex step for now
    if primary_intent == SkillIntent.MIXED:
        return decompose_simple(goal, primary_intent)
    
    # Default: single step
    return decompose_simple(goal, primary_intent)


def validate_plan(plan: SkillPlan) -> Tuple[bool, str]:
    """
    Validate a skill plan for safety and correctness.
    Returns (valid, error_message).
    """
    if not plan.steps:
        return False, "Plan has no steps"
    
    if len(plan.steps) > plan.max_steps:
        return False, f"Plan exceeds max_steps limit ({plan.max_steps})"
    
    step_ids = {step.step_id for step in plan.steps}
    
    for step in plan.steps:
        for dep in step.dependencies:
            if dep not in step_ids:
                return False, f"Step {step.step_id} depends on unknown step {dep}"
    
    if has_cycle(plan.steps):
        return False, "Plan contains dependency cycle"
    
    return True, ""
