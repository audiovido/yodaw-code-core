"""
Skill selection and ranking for YODAW Coder Skills.

Selects and ranks skills based on task classification, context, and constraints.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import (
    Skill,
    SkillId,
    Intent,
    SelectionResult,
    ClassificationResult,
    RiskLevel,
)
from .registry import SkillRegistry


@dataclass
class SelectionContext:
    """Context for skill selection."""
    task_description: str
    classification: ClassificationResult
    project_language: Optional[str] = None
    project_frameworks: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    available_tools: list[str] = field(default_factory=list)
    max_risk: Optional[RiskLevel] = None
    require_human_approval: bool = False


class SkillSelector:
    """Selects and ranks skills for a given task."""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def select(
        self,
        context: SelectionContext,
        max_alternatives: int = 3,
    ) -> SelectionResult:
        """
        Select the best skill for the task.

        Args:
            context: Selection context with task info and constraints
            max_alternatives: Maximum number of alternative skills to return

        Returns:
            SelectionResult with selected skill and alternatives
        """
        classification = context.classification

        # Get candidate skills
        candidates = self._get_candidates(context)

        if not candidates:
            # Fallback
            return SelectionResult(
                selected_skill=classification.primary_skill,
                alternatives=classification.secondary_skills[:max_alternatives],
                confidence=0.3,
                reason="No matching skills found in registry; using classification primary",
            )

        # Score candidates
        scored = self._score_candidates(candidates, context)

        # Sort by score
        scored.sort(key=lambda x: x[1], reverse=True)

        selected_skill = scored[0][0].id
        alternatives = [s.id for s, _ in scored[1:max_alternatives + 1]]
        confidence = scored[0][1]
        reason = self._generate_reason(scored[0][0], context, scored[:3])

        return SelectionResult(
            selected_skill=selected_skill,
            alternatives=alternatives,
            confidence=confidence,
            reason=reason,
        )

    def _get_candidates(self, context: SelectionContext) -> list[Skill]:
        """Get candidate skills based on context."""
        classification = context.classification

        # Start with skills matching the primary intent
        candidates = self.registry.get_by_intent(classification.detected_intent, enabled_only=True)

        # Filter by language if specified
        if context.project_language:
            candidates = [s for s in candidates if s.matches_language(context.project_language)]
        elif classification.detected_language:
            candidates = [s for s in candidates if s.matches_language(classification.detected_language)]

        # Filter by max risk
        if context.max_risk:
            risk_order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
            max_level = risk_order[context.max_risk]
            candidates = [s for s in candidates if risk_order[s.risk] <= max_level]

        # Filter by human approval requirement
        # If explicitly in automated mode (require_human_approval=False), prefer skills that don't require approval
        # But don't completely filter - let scoring handle it
        if context.require_human_approval is False:
            # In automated mode, we still consider all skills but the scoring will penalize
            pass

        # Add secondary intent skills if we have few candidates
        if len(candidates) < 3:
            for sec_skill_id in classification.secondary_skills:
                skill = self.registry.get(sec_skill_id)
                if skill and skill not in candidates:
                    candidates.append(skill)

        return candidates

    def _score_candidates(self, candidates: list[Skill], context: SelectionContext) -> list[tuple[Skill, float]]:
        """Score candidate skills."""
        scored = []
        classification = context.classification

        for skill in candidates:
            score = 0.0

            # Base score from classification confidence - MAJOR weight for primary skill
            if skill.id == classification.primary_skill:
                score += classification.confidence * 0.6
            elif skill.id in classification.secondary_skills:
                score += 0.4 * (1.0 - classification.secondary_skills.index(skill.id) * 0.1)

            # Language match bonus
            if context.project_language and skill.matches_language(context.project_language):
                score += 0.15
            elif classification.detected_language and skill.matches_language(classification.detected_language):
                score += 0.1

            # Framework match bonus
            for framework in context.project_frameworks:
                if framework.lower() in [t.lower() for t in skill.tags]:
                    score += 0.1

            # Risk penalty (lower risk preferred unless high risk explicitly needed)
            risk_penalty = {RiskLevel.LOW: 0.0, RiskLevel.MEDIUM: 0.05, RiskLevel.HIGH: 0.15, RiskLevel.CRITICAL: 0.3}
            score -= risk_penalty.get(skill.risk, 0.0)

            # Tool availability bonus
            if context.available_tools:
                # Check if skill prerequisites can be met
                prereq_met = sum(1 for p in skill.prerequisites if p.name in context.available_tools)
                score += min(prereq_met * 0.05, 0.15)

            # Preference bonuses
            if context.preferences.get("prefer_low_risk", False) and skill.risk == RiskLevel.LOW:
                score += 0.1
            if context.preferences.get("prefer_fast", False) and skill.planning_guidance.estimated_duration_minutes < 30:
                score += 0.1

            # Ensure minimum score
            score = max(score, 0.01)

            scored.append((skill, score))

        return scored

    def _generate_reason(self, skill: Skill, context: SelectionContext, top_candidates: list[tuple[Skill, float]]) -> str:
        """Generate selection reason."""
        parts = [
            f"Selected {skill.name} ({skill.id})",
            f"Intent: {context.classification.detected_intent.value}",
            f"Language: {context.project_language or context.classification.detected_language or 'any'}",
            f"Risk: {skill.risk.value}",
            f"Duration: ~{skill.planning_guidance.estimated_duration_minutes}min",
        ]

        if top_candidates:
            alt_names = [f"{s.name}({sc:.2f})" for s, sc in top_candidates[1:3]]
            if alt_names:
                parts.append(f"Alternatives: {', '.join(alt_names)}")

        return "; ".join(parts)

    def rank_skills(
        self,
        skills: list[Skill],
        context: SelectionContext,
    ) -> list[tuple[Skill, float]]:
        """
        Rank a list of skills for the given context.

        Args:
            skills: Skills to rank
            context: Selection context

        Returns:
            List of (skill, score) tuples sorted by score descending
        """
        scored = self._score_candidates(skills, context)
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def get_skill_chain(
        self,
        context: SelectionContext,
        max_skills: int = 5,
    ) -> list[Skill]:
        """
        Get a chain of skills for multi-step tasks.

        Args:
            context: Selection context
            max_skills: Maximum skills in chain

        Returns:
            Ordered list of skills
        """
        classification = context.classification

        chain = []

        # Primary skill
        primary = self.registry.get(classification.primary_skill)
        if primary:
            chain.append(primary)

        # Add secondary skills that complement
        for sec_id in classification.secondary_skills:
            if len(chain) >= max_skills:
                break
            skill = self.registry.get(sec_id)
            if skill and skill not in chain:
                chain.append(skill)

        # Add language-specific skills
        if context.project_language:
            lang_skills = self.registry.get_by_language(context.project_language, enabled_only=True)
            for skill in lang_skills:
                if len(chain) >= max_skills:
                    break
                if skill not in chain:
                    chain.append(skill)

        return chain[:max_skills]