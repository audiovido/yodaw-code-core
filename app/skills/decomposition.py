"""
Task decomposition for YODAW Coder Skills.

Decomposes complex tasks into structured, executable steps with proper
dependencies, risk assessment, and rollback planning.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import (
    Skill,
    SkillId,
    Intent,
    DecompositionStep,
    DecompositionResult,
    RiskLevel,
    ClassificationResult,
)
from .selection import SelectionContext
from .registry import SkillRegistry
from .selection import SkillSelector


class TaskDecomposer:
    """Decomposes tasks into executable skill steps."""

    # Default step templates for each intent
    STEP_TEMPLATES: dict[Intent, list[dict[str, Any]]] = {
        Intent.BUGFIX: [
            {"name": "reproduce", "description": "Reproduce the bug with a minimal test case", "duration": 30, "requires_approval": False},
            {"name": "diagnose", "description": "Diagnose root cause through analysis and debugging", "duration": 45, "requires_approval": False},
            {"name": "minimal_fix", "description": "Implement minimal fix addressing root cause", "duration": 30, "requires_approval": True},
            {"name": "targeted_tests", "description": "Write targeted tests for the fix", "duration": 30, "requires_approval": False},
            {"name": "broader_tests", "description": "Run broader test suite to check for regressions", "duration": 45, "requires_approval": False},
            {"name": "diff_review", "description": "Review diff for correctness and completeness", "duration": 15, "requires_approval": False},
            {"name": "commit", "description": "Commit fix with descriptive message", "duration": 5, "requires_approval": True},
        ],
        Intent.REFACTOR: [
            {"name": "analyze", "description": "Analyze current code structure and identify improvement areas", "duration": 30, "requires_approval": False},
            {"name": "plan", "description": "Create detailed refactoring plan with risk assessment", "duration": 20, "requires_approval": True},
            {"name": "setup_tests", "description": "Ensure comprehensive test coverage before changes", "duration": 30, "requires_approval": False},
            {"name": "execute", "description": "Execute refactoring in small, verified steps", "duration": 60, "requires_approval": True},
            {"name": "verify", "description": "Verify behavior preservation through tests", "duration": 30, "requires_approval": False},
            {"name": "review", "description": "Review changes for correctness and improvements", "duration": 20, "requires_approval": False},
            {"name": "commit", "description": "Commit refactored code", "duration": 5, "requires_approval": True},
        ],
        Intent.TEST: [
            {"name": "analyze", "description": "Analyze code to understand testing requirements", "duration": 20, "requires_approval": False},
            {"name": "design", "description": "Design test cases covering happy path, edge cases, and error conditions", "duration": 30, "requires_approval": True},
            {"name": "implement", "description": "Implement test cases with proper fixtures and mocks", "duration": 45, "requires_approval": False},
            {"name": "run", "description": "Run tests and verify they pass", "duration": 15, "requires_approval": False},
            {"name": "coverage", "description": "Check and improve test coverage", "duration": 20, "requires_approval": False},
            {"name": "commit", "description": "Commit tests", "duration": 5, "requires_approval": True},
        ],
        Intent.REVIEW: [
            {"name": "scope", "description": "Define review scope and criteria", "duration": 15, "requires_approval": False},
            {"name": "analyze", "description": "Perform code analysis (static, security, performance)", "duration": 45, "requires_approval": False},
            {"name": "findings", "description": "Document findings with severity ratings", "duration": 30, "requires_approval": False},
            {"name": "report", "description": "Generate review report with recommendations", "duration": 20, "requires_approval": False},
            {"name": "discuss", "description": "Discuss findings with stakeholders if needed", "duration": 30, "requires_approval": True},
        ],
        Intent.FEATURE: [
            {"name": "requirements", "description": "Analyze and clarify feature requirements", "duration": 30, "requires_approval": True},
            {"name": "design", "description": "Create technical design and API contracts", "duration": 45, "requires_approval": True},
            {"name": "implement", "description": "Implement feature in incremental steps", "duration": 90, "requires_approval": True},
            {"name": "test", "description": "Write and run tests for new functionality", "duration": 45, "requires_approval": False},
            {"name": "document", "description": "Update documentation and add examples", "duration": 30, "requires_approval": False},
            {"name": "review", "description": "Code review and integration verification", "duration": 30, "requires_approval": True},
            {"name": "commit", "description": "Commit feature implementation", "duration": 5, "requires_approval": True},
        ],
        Intent.DEPENDENCY: [
            {"name": "audit", "description": "Audit current dependencies for issues", "duration": 30, "requires_approval": False},
            {"name": "research", "description": "Research alternatives and evaluate options", "duration": 45, "requires_approval": False},
            {"name": "plan", "description": "Create migration/upgrade plan with rollback", "duration": 30, "requires_approval": True},
            {"name": "execute", "description": "Execute dependency changes", "duration": 45, "requires_approval": True},
            {"name": "test", "description": "Verify functionality after changes", "duration": 30, "requires_approval": False},
            {"name": "commit", "description": "Commit dependency changes", "duration": 5, "requires_approval": True},
        ],
        Intent.DOCUMENTATION: [
            {"name": "audit", "description": "Audit current documentation gaps", "duration": 20, "requires_approval": False},
            {"name": "plan", "description": "Plan documentation structure and content", "duration": 20, "requires_approval": True},
            {"name": "write", "description": "Write documentation content", "duration": 60, "requires_approval": False},
            {"name": "review", "description": "Review documentation for accuracy and clarity", "duration": 30, "requires_approval": False},
            {"name": "publish", "description": "Publish or commit documentation", "duration": 10, "requires_approval": True},
        ],
        Intent.MIGRATION: [
            {"name": "assess", "description": "Assess current state and migration scope", "duration": 45, "requires_approval": False},
            {"name": "plan", "description": "Create detailed migration plan with rollback", "duration": 60, "requires_approval": True},
            {"name": "prepare", "description": "Prepare environment and create backups", "duration": 30, "requires_approval": True},
            {"name": "execute", "description": "Execute migration in phases", "duration": 120, "requires_approval": True},
            {"name": "verify", "description": "Verify migration completeness and correctness", "duration": 45, "requires_approval": False},
            {"name": "cleanup", "description": "Clean up old code/infrastructure", "duration": 30, "requires_approval": True},
            {"name": "commit", "description": "Commit migration changes", "duration": 5, "requires_approval": True},
        ],
        Intent.PERFORMANCE: [
            {"name": "baseline", "description": "Establish performance baseline", "duration": 30, "requires_approval": False},
            {"name": "profile", "description": "Profile application to identify bottlenecks", "duration": 45, "requires_approval": False},
            {"name": "analyze", "description": "Analyze bottlenecks and prioritize optimizations", "duration": 30, "requires_approval": True},
            {"name": "optimize", "description": "Implement optimizations", "duration": 60, "requires_approval": True},
            {"name": "benchmark", "description": "Benchmark and verify improvements", "duration": 30, "requires_approval": False},
            {"name": "commit", "description": "Commit performance improvements", "duration": 5, "requires_approval": True},
        ],
        Intent.SECURITY: [
            {"name": "scan", "description": "Run security scans and vulnerability assessments", "duration": 30, "requires_approval": False},
            {"name": "analyze", "description": "Analyze findings and assess risk", "duration": 45, "requires_approval": False},
            {"name": "plan", "description": "Create remediation plan with prioritization", "duration": 30, "requires_approval": True},
            {"name": "remediate", "description": "Implement security fixes", "duration": 60, "requires_approval": True},
            {"name": "verify", "description": "Verify fixes and re-scan", "duration": 30, "requires_approval": False},
            {"name": "commit", "description": "Commit security fixes", "duration": 5, "requires_approval": True},
        ],
    }

    def __init__(self, registry: SkillRegistry, selector: SkillSelector):
        self.registry = registry
        self.selector = selector

    def decompose(
        self,
        context: SelectionContext,
        custom_steps: Optional[list[dict[str, Any]]] = None,
    ) -> DecompositionResult:
        """
        Decompose a task into executable steps.

        Args:
            context: Selection context with task info
            custom_steps: Optional custom step definitions to override defaults

        Returns:
            DecompositionResult with steps, estimates, and risk assessment
        """
        classification = context.classification
        intent = classification.detected_intent

        # Get step template
        template = custom_steps or self.STEP_TEMPLATES.get(intent, self.STEP_TEMPLATES[Intent.FEATURE])

        # Get primary skill
        primary_skill = self.registry.get(classification.primary_skill)

        steps = []
        step_id_map = {}

        for i, step_template in enumerate(template):
            step_id = f"step_{i+1}_{step_template['name']}"

            # Determine skill for this step
            step_skill_id = self._select_step_skill(step_template, classification, primary_skill)

            # Determine dependencies
            depends_on = []
            if i > 0:
                depends_on.append(f"step_{i}_{template[i-1]['name']}")

            # Check if rollback step needed
            rollback_step_id = None
            if step_template.get("requires_approval") and i > 0:
                rollback_step_id = f"step_{i}_{template[i-1]['name']}"

            step = DecompositionStep(
                step_id=step_id,
                skill_id=step_skill_id,
                description=step_template["description"],
                inputs={
                    "task_description": context.task_description,
                    "classification": classification,
                    "context": context.__dict__,
                    "step_config": step_template,
                },
                depends_on=depends_on,
                estimated_duration_minutes=step_template.get("duration", 30),
                requires_approval=step_template.get("requires_approval", False),
                rollback_step_id=rollback_step_id,
            )

            steps.append(step)
            step_id_map[step_id] = step

        # Calculate totals
        total_minutes = sum(s.estimated_duration_minutes for s in steps)
        requires_human_review = any(s.requires_approval for s in steps)

        # Determine overall risk level
        risk_level = self._assess_overall_risk(steps, primary_skill)

        return DecompositionResult(
            steps=steps,
            total_estimated_minutes=total_minutes,
            requires_human_review=requires_human_review,
            risk_level=risk_level,
            metadata={
                "intent": intent.value,
                "primary_skill": classification.primary_skill,
                "step_count": len(steps),
                "approval_points": sum(1 for s in steps if s.requires_approval),
            },
        )

    def _select_step_skill(
        self,
        step_template: dict[str, Any],
        classification: ClassificationResult,
        primary_skill: Optional[Skill],
    ) -> SkillId:
        """Select the appropriate skill for a step."""
        step_name = step_template["name"]

        # Map step names to skills
        step_skill_map = {
            "reproduce": "test",
            "diagnose": "review",
            "minimal_fix": "bugfix",
            "targeted_tests": "test",
            "broader_tests": "test",
            "diff_review": "review",
            "commit": classification.primary_skill,
            "analyze": "review",
            "plan": classification.primary_skill,
            "setup_tests": "test",
            "execute": classification.primary_skill,
            "verify": "test",
            "review": "review",
            "design": classification.primary_skill,
            "implement": classification.primary_skill,
            "test": "test",
            "coverage": "test",
            "scope": "review",
            "findings": "review",
            "report": "documentation",
            "discuss": "review",
            "requirements": "feature",
            "document": "documentation",
            "audit": "review",
            "research": "dependency",
            "prepare": classification.primary_skill,
            "cleanup": "refactor",
            "baseline": "performance",
            "profile": "performance",
            "optimize": "performance",
            "benchmark": "performance",
            "scan": "security",
            "remediate": "security",
        }

        skill_id = step_skill_map.get(step_name, classification.primary_skill)

        # Verify skill exists in registry
        if not self.registry.get(skill_id):
            skill_id = classification.primary_skill

        return skill_id

    def _assess_overall_risk(self, steps: list[DecompositionStep], primary_skill: Optional[Skill]) -> RiskLevel:
        """Assess overall risk level of the decomposition."""
        if not primary_skill:
            return RiskLevel.MEDIUM

        # Base risk from primary skill
        base_risk = primary_skill.risk

        # Increase risk if many approval points
        approval_count = sum(1 for s in steps if s.requires_approval)
        if approval_count > 4:
            risk_order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
            current = risk_order[base_risk]
            new_risk = min(current + 1, 3)
            return RiskLevel(list(risk_order.keys())[list(risk_order.values()).index(new_risk)])

        return base_risk

    def create_rollback_plan(self, decomposition: DecompositionResult) -> dict[str, Any]:
        """
        Create a rollback plan for the decomposition.

        Args:
            decomposition: The decomposition result

        Returns:
            Rollback plan with step-by-step instructions
        """
        rollback_steps = []

        for step in reversed(decomposition.steps):
            if step.rollback_step_id:
                rollback_steps.append({
                    "trigger_step": step.step_id,
                    "rollback_to": step.rollback_step_id,
                    "action": f"Rollback changes from {step.step_id}",
                    "commands": self._get_rollback_commands(step),
                })

        return {
            "decomposition_id": decomposition.metadata.get("intent", "unknown"),
            "rollback_steps": rollback_steps,
            "requires_manual_intervention": len(rollback_steps) > 0,
        }

    def _get_rollback_commands(self, step: DecompositionStep) -> list[str]:
        """Get rollback commands for a step."""
        # This would be customized per skill/step
        return [
            "git stash",
            "git checkout -- .",
            "# Manual verification required",
        ]