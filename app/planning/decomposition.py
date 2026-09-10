"""
Bounded Decomposition Engine

This module provides deterministic, bounded decomposition of goals into steps
without recursive task explosion. It uses a fixed set of patterns and templates.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Union
from dataclasses import dataclass, field
from uuid import uuid4
from app.planning.models import (
    Step, Plan, RiskLevel, ConflictClass, ValidationResult,
    Conflict, StepStatus
)


@dataclass
class DecompositionTemplate:
    """Template for a common decomposition pattern."""
    pattern: str  # regex pattern to match goal
    step_templates: list[dict[str, Any]]  # template steps
    max_depth: int = 3  # maximum decomposition depth
    max_steps: int = 20  # maximum steps per decomposition


# Built-in templates for common goal patterns
BUILTIN_TEMPLATES: list[DecompositionTemplate] = [
    DecompositionTemplate(
        pattern=r"(?i)(implement|add|create).*(feature|function|api|endpoint)",
        step_templates=[
            {"title": "Analyze requirements", "intent": "Understand the feature requirements",
             "description": "Parse the goal and identify specific requirements and constraints.",
             "parallel_safe": True, "risk": RiskLevel.LOW,
             "candidate_files": ["README.md", "ARCHITECTURE.md"],
             "read_set": ["README.md", "ARCHITECTURE.md"],
             "write_set": [],
             "validation": "Requirements documented",
             "evidence_requirements": ["requirements_doc"]},
            {"title": "Design implementation", "intent": "Create implementation design",
             "description": "Design the implementation approach, data models, and API contracts.",
             "parallel_safe": True, "risk": RiskLevel.MEDIUM,
             "dependencies": ["Analyze requirements"],
             "candidate_files": ["app/**/*.py"],
             "read_set": ["app/core/models.py", "app/api/"],
             "write_set": [],
             "validation": "Design document created",
             "evidence_requirements": ["design_doc"]},
            {"title": "Implement core logic", "intent": "Write the core implementation",
             "description": "Implement the main business logic for the feature.",
             "parallel_safe": False, "risk": RiskLevel.MEDIUM,
             "dependencies": ["Design implementation"],
             "candidate_files": ["app/**/*.py"],
             "read_set": ["app/core/models.py"],
             "write_set": ["app/core/*.py", "app/api/*.py"],
             "validation": "Core tests pass",
             "evidence_requirements": ["test_results"]},
            {"title": "Add API endpoints", "intent": "Expose functionality via API",
             "description": "Create REST API endpoints for the feature.",
             "parallel_safe": True, "risk": RiskLevel.MEDIUM,
             "dependencies": ["Implement core logic"],
             "candidate_files": ["app/api/*.py"],
             "read_set": ["app/api/"],
             "write_set": ["app/api/*.py"],
             "validation": "API tests pass",
             "evidence_requirements": ["api_test_results"]},
            {"title": "Write tests", "intent": "Ensure correctness with tests",
             "description": "Write unit and integration tests for the new feature.",
             "parallel_safe": True, "risk": RiskLevel.LOW,
             "dependencies": ["Implement core logic"],
             "candidate_files": ["tests/**/*.py"],
             "read_set": ["app/core/", "app/api/"],
             "write_set": ["tests/**/*.py"],
             "validation": "All tests pass",
             "evidence_requirements": ["test_coverage"]},
        ],
        max_depth=2,
        max_steps=10
    ),
    DecompositionTemplate(
        pattern=r"(?i)(fix|repair|debug).*(bug|issue|error|failure)",
        step_templates=[
            {"title": "Reproduce issue", "intent": "Reproduce the reported bug",
             "description": "Create a minimal reproduction case and understand the failure mode.",
             "parallel_safe": True, "risk": RiskLevel.LOW,
             "candidate_files": ["tests/**/*.py"],
             "read_set": ["tests/", "app/"],
             "write_set": [],
             "validation": "Bug reproduced consistently",
             "evidence_requirements": ["reproduction_steps"]},
            {"title": "Root cause analysis", "intent": "Identify the root cause",
             "description": "Analyze the code to find the underlying cause of the bug.",
             "parallel_safe": True, "risk": RiskLevel.MEDIUM,
             "dependencies": ["Reproduce issue"],
             "candidate_files": ["app/**/*.py"],
             "read_set": ["app/"],
             "write_set": [],
             "validation": "Root cause identified and documented",
             "evidence_requirements": ["root_cause_analysis"]},
            {"title": "Fix the bug", "intent": "Apply the minimal fix",
             "description": "Implement the fix addressing the root cause without side effects.",
             "parallel_safe": False, "risk": RiskLevel.HIGH,
             "dependencies": ["Root cause analysis"],
             "candidate_files": ["app/**/*.py"],
             "read_set": ["app/"],
             "write_set": ["app/**/*.py"],
             "validation": "Bug no longer reproduces; no regressions",
             "evidence_requirements": ["fix_verification", "regression_test_results"]},
            {"title": "Add regression test", "intent": "Prevent future regressions",
             "description": "Add a test that would have caught this bug.",
             "parallel_safe": True, "risk": RiskLevel.LOW,
             "dependencies": ["Implement fix"],
             "candidate_files": ["tests/**/*.py"],
             "read_set": ["app/"],
             "write_set": ["tests/**/*.py"],
             "validation": "Regression test passes",
             "evidence_requirements": ["regression_test"]},
        ],
        max_depth=2,
        max_steps=8
    ),
    DecompositionTemplate(
        pattern=r"(?i)(refactor|restructure|improve|optimize).*",
        step_templates=[
            {"title": "Analyze current state", "intent": "Understand current implementation",
             "description": "Map out the current code structure, dependencies, and pain points.",
             "parallel_safe": True, "risk": RiskLevel.LOW,
             "candidate_files": ["app/**/*.py"],
             "read_set": ["app/"],
             "write_set": [],
             "validation": "Current state documented",
             "evidence_requirements": ["analysis_doc"]},
            {"title": "Design refactor", "intent": "Plan the refactoring approach",
             "description": "Design the target architecture and identify safe refactoring steps.",
             "parallel_safe": True, "risk": RiskLevel.MEDIUM,
             "dependencies": ["Analyze current state"],
             "candidate_files": ["app/**/*.py"],
             "read_set": ["app/"],
             "write_set": [],
             "validation": "Refactor plan approved",
             "evidence_requirements": ["refactor_plan"]},
            {"title": "Execute refactor incrementally", "intent": "Apply refactoring in small steps",
             "description": "Apply the refactoring incrementally with validation at each step.",
             "parallel_safe": False, "risk": RiskLevel.HIGH,
             "dependencies": ["Design refactor"],
             "candidate_files": ["app/**/*.py"],
             "read_set": ["app/"],
             "write_set": ["app/**/*.py"],
             "validation": "All tests pass after each step",
             "evidence_requirements": ["incremental_test_results"]},
            {"title": "Validate no regressions", "intent": "Ensure behavior is preserved",
             "description": "Run full test suite to verify no behavioral changes.",
             "parallel_safe": True, "risk": RiskLevel.MEDIUM,
             "dependencies": ["Execute refactor incrementally"],
             "candidate_files": ["tests/**/*.py"],
             "read_set": ["tests/"],
             "write_set": [],
             "validation": "Full test suite passes",
             "evidence_requirements": ["full_test_results"]},
        ],
        max_depth=2,
        max_steps=8
    ),
    DecompositionTemplate(
        pattern=r"(?i)(test|testing|coverage).*",
        step_templates=[
            {"title": "Analyze test gaps", "intent": "Identify missing test coverage",
             "description": "Find untested code paths and critical functionality without tests.",
             "parallel_safe": True, "risk": RiskLevel.LOW,
             "candidate_files": ["app/**/*.py", "tests/**/*.py"],
             "read_set": ["app/", "tests/"],
             "write_set": [],
             "validation": "Coverage gaps documented",
             "evidence_requirements": ["coverage_report"]},
            {"title": "Write missing tests", "intent": "Add tests for uncovered code",
             "description": "Write unit and integration tests for identified gaps.",
             "parallel_safe": True, "risk": RiskLevel.LOW,
             "dependencies": ["Analyze test gaps"],
             "candidate_files": ["tests/**/*.py"],
             "read_set": ["app/"],
             "write_set": ["tests/**/*.py"],
             "validation": "Target coverage achieved",
             "evidence_requirements": ["test_coverage_report"]},
        ],
        max_depth=1,
        max_steps=5
    ),
]


class DecompositionEngine:
    """Engine for bounded goal decomposition."""
    
    def __init__(self, templates: Optional[list[DecompositionTemplate]] = None):
        self.templates = templates or BUILTIN_TEMPLATES
        self.max_total_steps = 50
        self.max_depth = 3
    
    def decompose(self, goal: str, context: Optional[dict[str, Any]] = None) -> Plan:
        """Decompose a goal into a structured plan."""
        context = context or {}
        
        # Find matching template
        template = self._match_template(goal)
        
        if template:
            steps = self._instantiate_steps(template, goal, context)
        else:
            # Fallback: generic decomposition
            steps = self._generic_decomposition(goal, context)
        
        # Build plan
        plan = Plan(
            goal=goal,
            summary=f"Auto-generated plan for: {goal}",
            steps=steps,
            validation_strategy=self._generate_validation_strategy(steps),
            merge_strategy=self._generate_merge_strategy(steps),
        )
        
        # Compute dependencies and risk
        self._compute_dependencies(plan)
        plan.compute_risk_summary()
        
        return plan
    
    def _match_template(self, goal: str) -> Optional[DecompositionTemplate]:
        """Find the best matching template for the goal."""
        for template in self.templates:
            if re.search(template.pattern, goal):
                return template
        return None
    
    def _instantiate_steps(
        self, 
        template: DecompositionTemplate, 
        goal: str, 
        context: dict[str, Any]
    ) -> list[Step]:
        """Instantiate step templates into concrete steps."""
        steps = []
        for i, tmpl in enumerate(template.step_templates):
            step = Step(
                id=f"step_{i+1}_{uuid4().hex[:6]}",
                title=tmpl.get("title", f"Step {i+1}"),
                intent=tmpl.get("intent", ""),
                description=tmpl.get("description", ""),
                dependencies=tmpl.get("dependencies", []),
                inputs=tmpl.get("inputs", {}),
                expected_outputs=tmpl.get("expected_outputs", {}),
                candidate_files=tmpl.get("candidate_files", []),
                read_set=tmpl.get("read_set", []),
                write_set=tmpl.get("write_set", []),
                shared_resources=tmpl.get("shared_resources", []),
                required_capabilities=tmpl.get("required_capabilities", []),
                validation=tmpl.get("validation", ""),
                parallel_safe=tmpl.get("parallel_safe", True),
                parallel_group=tmpl.get("parallel_group"),
                risk=tmpl.get("risk", RiskLevel.LOW),
                rollback=tmpl.get("rollback", ""),
                evidence_requirements=tmpl.get("evidence_requirements", []),
            )
            steps.append(step)
        
        # Assign parallel groups for parallel-safe steps with no mutual dependencies
        self._assign_parallel_groups(steps)
        return steps
    
    def _generic_decomposition(self, goal: str, context: dict[str, Any]) -> list[Step]:
        """Generic fallback decomposition."""
        steps = [
            Step(
                id=f"step_1_{uuid4().hex[:6]}",
                title="Analyze goal",
                intent="Understand the goal and identify approach",
                description=f"Analyze: {goal}",
                candidate_files=["README.md", "ARCHITECTURE.md"],
                read_set=["README.md", "ARCHITECTURE.md"],
                validation="Approach documented",
                evidence_requirements=["analysis"],
            ),
            Step(
                id=f"step_2_{uuid4().hex[:6]}",
                title="Plan implementation",
                intent="Create detailed implementation plan",
                description="Design the implementation with specific files and changes.",
                dependencies=["Analyze goal"],
                candidate_files=["app/**/*.py"],
                read_set=["app/"],
                write_set=[],
                validation="Plan complete",
                evidence_requirements=["plan_doc"],
            ),
            Step(
                id=f"step_3_{uuid4().hex[:6]}",
                title="Implement",
                intent="Execute the implementation plan",
                description="Make the necessary code changes.",
                dependencies=["Plan implementation"],
                candidate_files=["app/**/*.py"],
                read_set=["app/"],
                write_set=["app/**/*.py"],
                validation="Implementation complete",
                evidence_requirements=["code_changes"],
            ),
            Step(
                id=f"step_4_{uuid4().hex[:6]}",
                title="Test and validate",
                intent="Verify the implementation works correctly",
                description="Run tests and validate the implementation.",
                dependencies=["Implement"],
                candidate_files=["tests/**/*.py"],
                read_set=["tests/", "app/"],
                write_set=[],
                validation="All tests pass",
                evidence_requirements=["test_results"],
            ),
        ]
        # Assign parallel groups for generic decomposition too
        self._assign_parallel_groups(steps)
        return steps
    
    def _assign_parallel_groups(self, steps: list[Step]) -> None:
        """Assign parallel groups to steps that can safely run in parallel."""
        # Group parallel-safe steps by their dependency depth
        # Steps at the same depth with no mutual dependencies can run in parallel
        
        # Build title to step map
        title_to_step = {step.title: step for step in steps}
        
        # Compute depth for each step (longest path from root)
        step_depth = {}
        step_map = {step.id: step for step in steps}
        
        def compute_depth(step_id: str) -> int:
            if step_id in step_depth:
                return step_depth[step_id]
            step = step_map.get(step_id)
            if not step or not step.dependencies:
                step_depth[step_id] = 0
                return 0
            max_dep_depth = max(compute_depth(dep_id) for dep_id in step.dependencies)
            step_depth[step_id] = max_dep_depth + 1
            return step_depth[step_id]
        
        for step in steps:
            compute_depth(step.id)
        
        # Group by depth
        depth_groups: dict[int, list[Step]] = {}
        for step in steps:
            if step.parallel_safe:
                depth = step_depth[step.id]
                depth_groups.setdefault(depth, []).append(step)
        
        # Assign parallel group names
        for depth, group_steps in depth_groups.items():
            if len(group_steps) > 1:
                # Check for conflicts within the group
                conflict_free = True
                for i, step_a in enumerate(group_steps):
                    for step_b in group_steps[i+1:]:
                        conflict = self._check_conflict(step_a, step_b)
                        if conflict and conflict.conflict_class in (ConflictClass.HARD_CONFLICT, ConflictClass.POTENTIAL_CONFLICT):
                            conflict_free = False
                            break
                    if not conflict_free:
                        break
                
                if conflict_free:
                    group_name = f"parallel_group_{depth}"
                    for step in group_steps:
                        step.parallel_group = group_name
    
    def _compute_dependencies(self, plan: Plan) -> None:
        """Resolve dependency names to step IDs and build dependency graph."""
        title_to_id = {step.title: step.id for step in plan.steps}
        
        for step in plan.steps:
            resolved_deps = []
            for dep_title in step.dependencies:
                if dep_title in title_to_id:
                    resolved_deps.append(title_to_id[dep_title])
                else:
                    # Try partial match
                    for title, sid in title_to_id.items():
                        if dep_title.lower() in title.lower() or title.lower() in dep_title.lower():
                            resolved_deps.append(sid)
                            break
            step.dependencies = resolved_deps
        
        # Build adjacency list for validation
        plan.dependencies = [step.dependencies for step in plan.steps]
    
    def _generate_validation_strategy(self, steps: list[Step]) -> str:
        """Generate validation strategy description."""
        validations = [s.validation for s in steps if s.validation]
        return "Validation gates: " + "; ".join(validations) if validations else "No explicit validation gates"
    
    def _generate_merge_strategy(self, steps: list[Step]) -> str:
        """Generate merge/handoff strategy."""
        parallel_groups = set(s.parallel_group for s in steps if s.parallel_group)
        if parallel_groups:
            return f"Parallel groups: {', '.join(parallel_groups)}. Merge after each group completes."
        return "Sequential execution with validation gates"
    
    def validate_plan(self, plan: Plan) -> ValidationResult:
        """Validate a plan for correctness."""
        errors = []
        warnings = []
        
        # Check for cycles
        cycles = self._find_cycles(plan)
        if cycles:
            errors.append(f"Cycles detected: {cycles}")
        
        # Check for orphans (steps with no dependencies and no dependents)
        orphans = self._find_orphans(plan)
        if orphans:
            warnings.append(f"Orphan steps: {orphans}")
        
        # Find critical path
        critical_path = self._find_critical_path(plan)
        
        # Find parallel groups
        parallel_groups = {}
        for step in plan.steps:
            if step.parallel_group:
                parallel_groups.setdefault(step.parallel_group, []).append(step.id)
        
        # Detect conflicts
        conflicts = self._detect_conflicts(plan)
        
        # Topological order
        topo_order = self._topological_sort(plan)
        
        valid = len(errors) == 0
        
        return ValidationResult(
            valid=valid,
            errors=errors,
            warnings=warnings,
            topological_order=topo_order,
            cycles=cycles,
            orphans=orphans,
            critical_path=critical_path,
            parallel_groups=parallel_groups,
            conflicts=conflicts,
        )
    
    def _find_cycles(self, plan: Plan) -> list[list[str]]:
        """Detect cycles in the dependency graph using DFS."""
        visited = set()
        rec_stack = set()
        path = []
        cycles = []
        
        step_map = {step.id: step for step in plan.steps}
        
        def dfs(node_id: str):
            visited.add(node_id)
            rec_stack.add(node_id)
            path.append(node_id)
            
            step = step_map.get(node_id)
            if step:
                for dep_id in step.dependencies:
                    if dep_id not in visited:
                        dfs(dep_id)
                    elif dep_id in rec_stack:
                        # Found cycle
                        cycle_start = path.index(dep_id)
                        cycles.append(path[cycle_start:] + [dep_id])
            
            rec_stack.remove(node_id)
            path.pop()
        
        for step in plan.steps:
            if step.id not in visited:
                dfs(step.id)
        
        return cycles
    
    def _find_orphans(self, plan: Plan) -> list[str]:
        """Find steps with no dependencies and no dependents."""
        step_map = {step.id: step for step in plan.steps}
        has_dependent = set()
        
        for step in plan.steps:
            for dep_id in step.dependencies:
                has_dependent.add(dep_id)
        
        orphans = []
        for step in plan.steps:
            if not step.dependencies and step.id not in has_dependent:
                orphans.append(step.id)
        
        return orphans
    
    def _find_critical_path(self, plan: Plan) -> list[str]:
        """Find the critical path (longest dependency chain)."""
        # First check for cycles - if cycles exist, we can't compute a meaningful critical path
        cycles = self._find_cycles(plan)
        if cycles:
            # Return empty path or a warning path when cycles exist
            return []
        
        step_map = {step.id: step for step in plan.steps}
        memo = {}
        
        def longest_path(node_id: str) -> list[str]:
            if node_id in memo:
                return memo[node_id]
            
            step = step_map.get(node_id)
            if not step or not step.dependencies:
                memo[node_id] = [node_id]
                return memo[node_id]
            
            best = []
            for dep_id in step.dependencies:
                path = longest_path(dep_id)
                if len(path) > len(best):
                    best = path
            
            memo[node_id] = best + [node_id]
            return memo[node_id]
        
        best_path = []
        for step in plan.steps:
            path = longest_path(step.id)
            if len(path) > len(best_path):
                best_path = path
        
        return best_path
    
    def _topological_sort(self, plan: Plan) -> list[str]:
        """Return a topological ordering of steps using Kahn's algorithm."""
        step_map = {step.id: step for step in plan.steps}
        in_degree = {step.id: 0 for step in plan.steps}
        
        # Build in-degree: for each step, count its dependencies
        for step in plan.steps:
            for dep_id in step.dependencies:
                if dep_id in in_degree:
                    in_degree[step.id] += 1
                else:
                    # Dependency not in plan, ignore for sorting
                    pass
        
        # Use deque for O(1) popleft
        from collections import deque
        queue = deque([sid for sid, deg in in_degree.items() if deg == 0])
        result = []
        
        while queue:
            node = queue.popleft()
            result.append(node)
            
            step = step_map.get(node)
            if step:
                # Find steps that depend on this node
                for other_step in plan.steps:
                    if node in other_step.dependencies:
                        in_degree[other_step.id] -= 1
                        if in_degree[other_step.id] == 0:
                            queue.append(other_step.id)
        
        # If there are remaining nodes with in_degree > 0, there's a cycle
        # But we still return what we have for partial order
        if len(result) < len(plan.steps):
            # Add remaining nodes
            remaining = [sid for sid in plan.steps if sid.id not in result]
            result.extend([s.id for s in remaining])
        
        return result
        
        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        result = []
        
        while queue:
            node = queue.pop(0)
            result.append(node)
            
            step = step_map.get(node)
            if step:
                for dep_id in step.dependencies:
                    if dep_id in in_degree:
                        in_degree[dep_id] -= 1
                        if in_degree[dep_id] == 0:
                            queue.append(dep_id)
        
        if len(result) != len(plan.steps):
            # Cycle detected, return what we have
            pass
        
        return result
    
    def _detect_conflicts(self, plan: Plan) -> list[Conflict]:
        """Detect conflicts between steps based on read/write sets."""
        conflicts = []
        
        for i, step_a in enumerate(plan.steps):
            for step_b in plan.steps[i+1:]:
                conflict = self._check_conflict(step_a, step_b)
                if conflict:
                    conflicts.append(conflict)
        
        return conflicts
    
    def _check_conflict(self, step_a: Step, step_b: Step) -> Optional[Conflict]:
        """Check if two steps have conflicting read/write sets."""
        # Same file in write sets = HARD_CONFLICT
        write_intersection = set(step_a.write_set) & set(step_b.write_set)
        if write_intersection:
            return Conflict(
                step_a=step_a.id,
                step_b=step_b.id,
                conflict_class=ConflictClass.HARD_CONFLICT,
                reason=f"Both steps write to: {', '.join(write_intersection)}",
                shared_resource=", ".join(write_intersection),
            )
        
        # One writes, other reads = POTENTIAL_CONFLICT
        read_a = set(step_a.read_set)
        write_b = set(step_b.write_set)
        read_b = set(step_b.read_set)
        write_a = set(step_a.write_set)
        
        if write_a & read_b:
            return Conflict(
                step_a=step_a.id,
                step_b=step_b.id,
                conflict_class=ConflictClass.POTENTIAL_CONFLICT,
                reason=f"Step {step_a.id} writes to files read by {step_b.id}",
                shared_resource=", ".join(write_a & read_b),
            )
        
        if write_b & read_a:
            return Conflict(
                step_a=step_a.id,
                step_b=step_b.id,
                conflict_class=ConflictClass.POTENTIAL_CONFLICT,
                reason=f"Step {step_b.id} writes to files read by {step_a.id}",
                shared_resource=", ".join(write_b & read_a),
            )
        
        # Shared resources = READ_ONLY_COMPATIBLE if both only read
        shared = (set(step_a.shared_resources) | set(step_a.read_set)) & \
                 (set(step_b.shared_resources) | set(step_b.read_set))
        if shared and not write_intersection and not (write_a & read_b) and not (write_b & read_a):
            return Conflict(
                step_a=step_a.id,
                step_b=step_b.id,
                conflict_class=ConflictClass.READ_ONLY_COMPATIBLE,
                reason=f"Both steps read shared resources: {', '.join(shared)}",
                shared_resource=", ".join(shared),
            )
        
        return None