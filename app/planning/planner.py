"""
Advanced Planner - Main Planning Interface

This module provides the high-level planning interface that integrates
decomposition, validation, conflict analysis, and execution planning.
"""

from __future__ import annotations

from typing import Any, Optional, Union
from app.planning.models import (
    Plan, Step, ValidationResult, Conflict, ConflictClass,
    RiskLevel, StepStatus, HandoffPackage, Checkpoint,
    RecoveryPlan, PlanRevision
)
from app.planning.decomposition import DecompositionEngine


class AdvancedPlanner:
    """Advanced planner with DAG validation, conflict analysis, and parallel safety."""
    
    def __init__(self):
        self.engine = DecompositionEngine()
        self.plans: dict[str, Plan] = {}
        self.checkpoints: dict[str, list[Checkpoint]] = {}
        self.revisions: dict[str, list[PlanRevision]] = {}
        self.completed_history: list[dict[str, Any]] = []
    
    def create_plan(self, goal: str, context: Optional[dict[str, Any]] = None) -> Plan:
        """Create a new plan from a goal."""
        plan = self.engine.decompose(goal, context)
        self.plans[plan.id] = plan
        self.checkpoints[plan.id] = []
        self.revisions[plan.id] = []
        return plan
    
    def validate_plan(self, plan: Plan) -> ValidationResult:
        """Validate a plan."""
        return self.engine.validate_plan(plan)
    
    def get_execution_order(self, plan: Plan) -> list[list[Step]]:
        """Get execution order grouped by parallel-safety."""
        validation = self.validate_plan(plan)
        
        if not validation.valid:
            raise ValueError(f"Plan invalid: {validation.errors}")
        
        # Group by topological level
        topo_order = validation.topological_order
        step_map = {step.id: step for step in plan.steps}
        
        # Build levels
        levels: list[list[Step]] = []
        remaining = set(topo_order)
        completed = set()
        
        while remaining:
            level = []
            for step_id in list(remaining):
                step = step_map[step_id]
                if all(dep in completed for dep in step.dependencies):
                    level.append(step)
                    remaining.remove(step_id)
            
            if not level:
                # Should not happen if no cycles
                break
            
            levels.append(level)
            completed.update(s.id for s in level)
        
        return levels
    
    def get_parallel_groups(self, plan: Plan) -> dict[str, list[Step]]:
        """Get steps grouped by parallel_group."""
        groups: dict[str, list[Step]] = {}
        for step in plan.steps:
            if step.parallel_group:
                groups.setdefault(step.parallel_group, []).append(step)
        return groups
    
    def analyze_conflicts(self, plan: Plan) -> list[Conflict]:
        """Analyze all conflicts in the plan."""
        return self.engine._detect_conflicts(plan)
    
    def get_hard_conflicts(self, plan: Plan) -> list[Conflict]:
        """Get only hard conflicts (same file writes)."""
        return [c for c in self.analyze_conflicts(plan) 
                if c.conflict_class == ConflictClass.HARD_CONFLICT]
    
    def is_parallel_safe(self, step_a: Step, step_b: Step) -> bool:
        """Check if two steps can run in parallel."""
        if not step_a.parallel_safe or not step_b.parallel_safe:
            return False
        
        conflict = self.engine._check_conflict(step_a, step_b)
        if conflict is None:
            return True
        
        return conflict.conflict_class in (
            ConflictClass.NO_CONFLICT,
            ConflictClass.READ_ONLY_COMPATIBLE,
        )
    
    def create_checkpoint(self, plan_id: str, step_id: str, 
                         step_status: StepStatus,
                         completed_steps: list[str],
                         evidence: Optional[list[dict[str, Any]]] = None,
                         outputs: Optional[dict[str, Any]] = None) -> Checkpoint:
        """Create a recovery checkpoint."""
        cp = Checkpoint(
            plan_id=plan_id,
            step_id=step_id,
            step_status=step_status,
            completed_steps=completed_steps,
            evidence=evidence or [],
            outputs=outputs or {},
        )
        self.checkpoints[plan_id].append(cp)
        return cp
    
    def get_latest_checkpoint(self, plan_id: str) -> Optional[Checkpoint]:
        """Get the latest checkpoint for a plan."""
        checkpoints = self.checkpoints.get(plan_id, [])
        return checkpoints[-1] if checkpoints else None
    
    def create_recovery_plan(self, plan_id: str, failed_step_id: str,
                           failure_reason: str) -> RecoveryPlan:
        """Create a recovery plan from a failure."""
        plan = self.plans.get(plan_id)
        if not plan:
            raise ValueError(f"Plan {plan_id} not found")
        
        checkpoint = self.get_latest_checkpoint(plan_id)
        
        recovery = RecoveryPlan(
            original_plan_id=plan_id,
            failed_step_id=failed_step_id,
            failure_reason=failure_reason,
            checkpoint=checkpoint,
        )
        
        # Add rollback steps for completed steps after the failure point
        if checkpoint:
            failed_idx = plan.steps.index(next(s for s in plan.steps if s.id == failed_step_id))
            for step in plan.steps[:failed_idx]:
                if step.status == StepStatus.COMPLETED and step.rollback:
                    rollback_step = Step(
                        title=f"Rollback: {step.title}",
                        intent=f"Rollback changes from {step.title}",
                        description=step.rollback,
                        dependencies=[],
                        rollback="",
                        parallel_safe=False,
                        risk=RiskLevel.HIGH,
                    )
                    recovery.rollback_steps.append(rollback_step)
        
        return recovery
    
    def revise_plan(self, plan: Plan, reason: str,
                   changed_steps: Optional[list[str]] = None,
                   added_steps: Optional[list[str]] = None,
                   removed_steps: Optional[list[str]] = None) -> PlanRevision:
        """Record a plan revision."""
        revision = PlanRevision(
            plan_id=plan.id,
            revision=plan.revision + 1,
            changed_steps=changed_steps or [],
            added_steps=added_steps or [],
            removed_steps=removed_steps or [],
            reason=reason,
            previous_revision=plan.revision,
        )
        
        plan.revision += 1
        plan.updated_at = plan.__fields__["updated_at"].default_factory()
        
        self.revisions[plan.id].append(revision)
        return revision
    
    def record_completion(self, plan_id: str, step_id: str,
                         evidence: list[dict[str, Any]],
                         outputs: dict[str, Any]) -> None:
        """Record step completion for history."""
        self.completed_history.append({
            "plan_id": plan_id,
            "step_id": step_id,
            "evidence": evidence,
            "outputs": outputs,
            "timestamp": self._now_iso(),
        })
    
    def _now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()
    
    def get_plan_summary(self, plan: Plan) -> dict[str, Any]:
        """Get a comprehensive summary of the plan."""
        validation = self.validate_plan(plan)
        
        return {
            "plan_id": plan.id,
            "goal": plan.goal,
            "summary": plan.summary,
            "total_steps": len(plan.steps),
            "revision": plan.revision,
            "risk_summary": plan.risk_summary,
            "validation": {
                "valid": validation.valid,
                "errors": validation.errors,
                "warnings": validation.warnings,
                "topological_order": validation.topological_order,
                "cycles": validation.cycles,
                "orphans": validation.orphans,
                "critical_path": validation.critical_path,
                "parallel_groups": validation.parallel_groups,
                "conflicts": [
                    {
                        "step_a": c.step_a,
                        "step_b": c.step_b,
                        "class": c.conflict_class.value,
                        "reason": c.reason,
                        "resource": c.shared_resource,
                    }
                    for c in validation.conflicts
                ],
            },
            "execution_levels": [
                [s.id for s in level] 
                for level in self.get_execution_order(plan)
            ],
            "parallel_groups": {
                k: [s.id for s in v] 
                for k, v in self.get_parallel_groups(plan).items()
            },
            "checkpoints": len(self.checkpoints.get(plan.id, [])),
            "revisions": len(self.revisions.get(plan.id, [])),
        }
    
    def export_plan(self, plan: Plan) -> dict[str, Any]:
        """Export plan as a serializable dictionary."""
        return {
            "plan": plan.model_dump(),
            "validation": self.validate_plan(plan).model_dump(),
            "summary": self.get_plan_summary(plan),
        }


# Convenience function
def plan(goal: str, context: Optional[dict[str, Any]] = None) -> Plan:
    """Quick planning function."""
    planner = AdvancedPlanner()
    return planner.create_plan(goal, context)