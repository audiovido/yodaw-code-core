"""
Skill-aware planning integration for YODAW Coder.

Integrates the skill system with the existing coder planning flow,
providing skill classification, decomposition, and skill-specific guidance.
"""
from typing import Optional
from pathlib import Path

from app.skills.classifier import classify_task
from app.skills.decomposition import decompose_task, validate_plan
from app.skills.registry import select_skill, get_skill
from app.skills.adapters import detect_project_adapter
from app.skills.protocols import SkillIntent, SkillClassification, SkillPlan


def classify_and_select_skill(goal: str, provider=None) -> tuple:
    """
    Classify task and select appropriate skill.
    
    Returns (classification, skill) where skill may be None if unsupported.
    """
    classification = classify_task(goal, provider=provider)
    skill = select_skill(classification.primary_skill)
    
    return classification, skill


def build_skill_context(
    goal: str,
    worktree: Path,
    classification: SkillClassification,
    provider=None,
) -> str:
    """
    Build additional planning context from skill system.
    
    Returns skill-specific guidance and project adapter context.
    """
    context_parts = []
    
    # Skill guidance
    skill = select_skill(classification.primary_skill)
    if skill:
        context_parts.append(
            f"\n--- SKILL GUIDANCE: {skill.skill_id.upper()} ---\n"
        )
        context_parts.append(skill.planning_guidance())
        context_parts.append("\n")
    
    # Project adapter context
    adapter = detect_project_adapter(worktree)
    if adapter:
        context_parts.append(
            f"\n--- PROJECT TYPE: {adapter.language.upper()} ---\n"
        )
        
        test_commands = adapter.test_commands(worktree)
        if test_commands:
            context_parts.append(
                f"Detected test commands: {test_commands}\n"
            )
        
        dep_files = adapter.dependency_files(worktree)
        if dep_files:
            context_parts.append(
                f"Dependency files: {dep_files}\n"
            )
    
    return "".join(context_parts)


def decompose_if_needed(
    goal: str,
    classification: SkillClassification,
) -> Optional[SkillPlan]:
    """
    Decompose goal into steps if it requires multi-step execution.
    
    Returns None for single-step goals, SkillPlan for multi-step.
    """
    if classification.primary_skill == SkillIntent.MIXED:
        # Mixed intent may need decomposition
        pass
    
    # Check for explicit multi-step patterns
    secondary = classification.secondary_skills
    
    if not secondary:
        # Single-step goal
        return None
    
    # Decompose based on primary + secondary intents
    plan = decompose_task(
        goal,
        classification.primary_skill,
        secondary,
    )
    
    # If plan is just one step, don't bother with decomposition
    if len(plan.steps) <= 1:
        return None
    
    # Validate plan
    valid, error = validate_plan(plan)
    if not valid:
        # Plan validation failed; fall back to single-step
        return None
    
    return plan


def format_skill_evidence(
    classification: SkillClassification,
    plan: Optional[SkillPlan] = None,
) -> dict:
    """
    Format skill classification and plan as structured evidence.
    """
    evidence = {
        "type": "skill_classification",
        "primary_skill": classification.primary_skill.value,
        "secondary_skills": [s.value for s in classification.secondary_skills],
        "confidence": classification.confidence,
        "reason": classification.reason,
    }
    
    if plan:
        evidence["plan"] = {
            "steps": [
                {
                    "step_id": step.step_id,
                    "description": step.description,
                    "skill_hint": step.skill_hint.value if step.skill_hint else None,
                    "dependencies": step.dependencies,
                    "parallel_safe": step.parallel_safe,
                }
                for step in plan.steps
            ],
            "bounded": plan.bounded,
            "max_steps": plan.max_steps,
        }
    
    return evidence


def enhance_coder_prompt(
    base_prompt: str,
    goal: str,
    worktree: Path,
    provider=None,
) -> tuple:
    """
    Enhance base coder prompt with skill-aware context.
    
    Returns (enhanced_prompt, skill_evidence).
    """
    # Classify task
    classification, skill = classify_and_select_skill(goal, provider)
    
    # Build skill context
    skill_context = build_skill_context(
        goal,
        worktree,
        classification,
        provider,
    )
    
    # Decompose if needed (for now we don't modify the prompt for multi-step)
    plan = decompose_if_needed(goal, classification)
    
    # Enhance prompt
    enhanced = base_prompt
    if skill_context:
        enhanced = base_prompt + "\n" + skill_context
    
    # Format evidence
    evidence = format_skill_evidence(classification, plan)
    
    return enhanced, evidence
