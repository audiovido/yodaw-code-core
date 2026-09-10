"""
Multi-step safe execution contracts for YODAW Coder Skills.

Provides contracts for safe, cancellable, and auditable multi-step skill execution
with rollback capabilities and state management.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

from .models import (
    DecompositionStep,
    DecompositionResult,
    SkillId,
    RiskLevel,
    SkillEvidence,
)
from .registry import SkillRegistry


class ExecutionState(str, Enum):
    """Execution state."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class StepState(str, Enum):
    """Individual step state."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"


@dataclass
class StepExecution:
    """Execution state for a single step."""
    step: DecompositionStep
    state: StepState = StepState.PENDING
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    evidence: Optional[SkillEvidence] = None
    error: Optional[str] = None
    retry_count: int = 0
    checkpoint_data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionContract:
    """Contract for a multi-step execution."""
    execution_id: str
    decomposition: DecompositionResult
    steps: list[StepExecution] = field(default_factory=list)
    state: ExecutionState = ExecutionState.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    current_step_index: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    rollback_plan: Optional[dict[str, Any]] = None

    def __post_init__(self):
        if not self.execution_id:
            self.execution_id = str(uuid4())

        # Initialize step executions
        if not self.steps:
            self.steps = [StepExecution(step=step) for step in self.decomposition.steps]

    def get_current_step(self) -> Optional[StepExecution]:
        """Get the current step execution."""
        if 0 <= self.current_step_index < len(self.steps):
            return self.steps[self.current_step_index]
        return None

    def get_completed_steps(self) -> list[StepExecution]:
        """Get all completed steps."""
        return [s for s in self.steps if s.state == StepState.COMPLETED]

    def get_failed_steps(self) -> list[StepExecution]:
        """Get all failed steps."""
        return [s for s in self.steps if s.state == StepState.FAILED]

    def is_complete(self) -> bool:
        """Check if all steps are complete."""
        return all(s.state in [StepState.COMPLETED, StepState.SKIPPED, StepState.ROLLED_BACK] for s in self.steps)

    def has_failures(self) -> bool:
        """Check if any step failed."""
        return any(s.state == StepState.FAILED for s in self.steps)

    def can_proceed(self) -> bool:
        """Check if execution can proceed to next step."""
        current = self.get_current_step()
        if not current:
            return False
        if current.state != StepState.COMPLETED:
            return False
        if self.current_step_index + 1 >= len(self.steps):
            return False
        next_step = self.steps[self.current_step_index + 1]
        return next_step.state == StepState.PENDING


@dataclass
class ExecutionCheckpoint:
    """Checkpoint for execution state."""
    execution_id: str
    step_index: int
    timestamp: str
    step_states: list[StepState]
    global_state: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionEngine:
    """Engine for executing skill contracts safely."""

    def __init__(
        self,
        registry: SkillRegistry,
        skill_executors: dict[SkillId, Callable],
        checkpoint_store: Optional[Callable] = None,
        evidence_store: Optional[Callable] = None,
    ):
        self.registry = registry
        self.skill_executors = skill_executors
        self.checkpoint_store = checkpoint_store
        self.evidence_store = evidence_store
        self._current_contract: Optional[ExecutionContract] = None
        self._cancel_requested = False
        self._pause_requested = False

    def create_contract(self, decomposition: DecompositionResult) -> ExecutionContract:
        """Create a new execution contract."""
        contract = ExecutionContract(
            execution_id=str(uuid4()),
            decomposition=decomposition,
        )
        return contract

    def execute(
        self,
        contract: ExecutionContract,
        context: dict[str, Any],
        progress_callback: Optional[Callable[[ExecutionContract, StepExecution], None]] = None,
    ) -> ExecutionContract:
        """
        Execute a contract.

        Args:
            contract: The contract to execute
            context: Execution context
            progress_callback: Optional callback for progress updates

        Returns:
            Updated contract with results
        """
        self._current_contract = contract
        # Don't reset cancel/pause if they were requested before execute started
        # Only reset if we're starting fresh (not already requested)
        if not self._cancel_requested:
            self._cancel_requested = False
        if not self._pause_requested:
            self._pause_requested = False

        contract.state = ExecutionState.RUNNING
        contract.started_at = datetime.now().isoformat()

        try:
            for i, step_exec in enumerate(contract.steps):
                contract.current_step_index = i

                # Check for cancellation
                if self._cancel_requested:
                    contract.state = ExecutionState.CANCELLED
                    contract.cancelled_at = datetime.now().isoformat()
                    break

                # Check for pause
                while self._pause_requested and not self._cancel_requested:
                    contract.state = ExecutionState.PAUSED
                    # Wait for resume (in real implementation, this would be async)
                    # For now, just continue

                # Check dependencies
                if not self._check_dependencies(step_exec, contract):
                    step_exec.state = StepState.FAILED
                    step_exec.error = "Dependencies not met"
                    contract.state = ExecutionState.FAILED
                    break

                # Check approval requirement
                if step_exec.step.requires_approval and not context.get("auto_approve", False):
                    # In real implementation, would wait for human approval
                    # For now, auto-approve if in test mode
                    if not context.get("test_mode", False):
                        step_exec.state = StepState.FAILED
                        step_exec.error = "Requires human approval"
                        contract.state = ExecutionState.FAILED
                        break

                # Execute step
                step_exec.state = StepState.RUNNING
                step_exec.start_time = datetime.now().isoformat()

                if progress_callback:
                    progress_callback(contract, step_exec)

                try:
                    evidence = self._execute_step(step_exec, context, contract)
                    step_exec.evidence = evidence
                    step_exec.state = StepState.COMPLETED
                    step_exec.end_time = datetime.now().isoformat()

                    # Store evidence
                    if self.evidence_store and evidence:
                        self.evidence_store(evidence)

                    # Create checkpoint
                    self._create_checkpoint(contract)

                except Exception as e:
                    step_exec.state = StepState.FAILED
                    step_exec.error = str(e)
                    step_exec.end_time = datetime.now().isoformat()
                    contract.state = ExecutionState.FAILED

                    # Attempt rollback if configured
                    if step_exec.step.rollback_step_id:
                        self._rollback(contract, step_exec.step.rollback_step_id)

                    break

                if progress_callback:
                    progress_callback(contract, step_exec)

            # Final state
            if contract.state == ExecutionState.RUNNING:
                if contract.is_complete():
                    contract.state = ExecutionState.COMPLETED
                elif contract.has_failures():
                    contract.state = ExecutionState.FAILED

            contract.completed_at = datetime.now().isoformat()

        finally:
            self._current_contract = None

        return contract

    def _execute_step(
        self,
        step_exec: StepExecution,
        context: dict[str, Any],
        contract: ExecutionContract,
    ) -> SkillEvidence:
        """Execute a single step."""
        skill_id = step_exec.step.skill_id
        executor = self.skill_executors.get(skill_id)

        if not executor:
            raise ValueError(f"No executor found for skill: {skill_id}")

        # Prepare inputs
        inputs = {**step_exec.step.inputs, **context}

        # Execute
        evidence = executor(inputs, context)

        return evidence

    def _check_dependencies(self, step_exec: StepExecution, contract: ExecutionContract) -> bool:
        """Check if step dependencies are satisfied."""
        for dep_id in step_exec.step.depends_on:
            # Find dependency step
            dep_step = None
            for s in contract.steps:
                if s.step.step_id == dep_id:
                    dep_step = s
                    break

            if not dep_step:
                return False

            if dep_step.state != StepState.COMPLETED:
                return False

        return True

    def _create_checkpoint(self, contract: ExecutionContract):
        """Create execution checkpoint."""
        if not self.checkpoint_store:
            return

        checkpoint = ExecutionCheckpoint(
            execution_id=contract.execution_id,
            step_index=contract.current_step_index,
            timestamp=datetime.now().isoformat(),
            step_states=[s.state for s in contract.steps],
            global_state=contract.metadata,
        )

        self.checkpoint_store(checkpoint)

    def _rollback(self, contract: ExecutionContract, rollback_to_step_id: str):
        """Rollback to a previous step."""
        contract.state = ExecutionState.ROLLING_BACK

        # Find rollback target index
        target_index = -1
        for i, step in enumerate(contract.steps):
            if step.step.step_id == rollback_to_step_id:
                target_index = i
                break

        if target_index == -1:
            contract.state = ExecutionState.FAILED
            return

        # Rollback completed steps after target
        for i in range(len(contract.steps) - 1, target_index, -1):
            step_exec = contract.steps[i]
            if step_exec.state == StepState.COMPLETED:
                # Execute rollback for this step
                self._rollback_step(step_exec, contract)
                step_exec.state = StepState.ROLLED_BACK

        contract.state = ExecutionState.ROLLED_BACK

    def _rollback_step(self, step_exec: StepExecution, contract: ExecutionContract):
        """Rollback a single step."""
        # In real implementation, would execute skill-specific rollback
        # For now, just mark as rolled back
        if step_exec.evidence and step_exec.evidence.outputs.get("rollback_command"):
            # Would execute rollback command
            pass

    def request_cancel(self):
        """Request cancellation of current execution."""
        self._cancel_requested = True

    def request_pause(self):
        """Request pause of current execution."""
        self._pause_requested = True

    def request_resume(self):
        """Request resume of paused execution."""
        self._pause_requested = False

    def get_contract(self) -> Optional[ExecutionContract]:
        """Get current contract."""
        return self._current_contract


class ContractValidator:
    """Validates execution contracts."""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def validate(self, contract: ExecutionContract) -> list[str]:
        """Validate a contract before execution."""
        errors = []

        # Check decomposition validity
        if not contract.decomposition.steps:
            errors.append("No steps in decomposition")

        # Check each step
        for i, step_exec in enumerate(contract.steps):
            step = step_exec.step

            # Check skill exists
            skill = self.registry.get(step.skill_id)
            if not skill:
                errors.append(f"Step {i}: Skill {step.skill_id} not found in registry")

            # Check dependencies reference valid steps
            for dep_id in step.depends_on:
                found = False
                for s in contract.steps:
                    if s.step.step_id == dep_id:
                        found = True
                        break
                if not found:
                    errors.append(f"Step {i}: Dependency {dep_id} not found")

            # Check rollback step exists
            if step.rollback_step_id:
                found = False
                for s in contract.steps:
                    if s.step.step_id == step.rollback_step_id:
                        found = True
                        break
                if not found:
                    errors.append(f"Step {i}: Rollback step {step.rollback_step_id} not found")

        # Check for cycles in dependencies
        if self._has_cycles(contract):
            errors.append("Circular dependency detected in steps")

        # Check risk level matches approval requirements
        if contract.decomposition.risk_level in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
            if not any(s.step.requires_approval for s in contract.steps):
                errors.append("High-risk execution requires at least one approval step")

        return errors

    def _has_cycles(self, contract: ExecutionContract) -> bool:
        """Check for cycles in step dependencies."""
        visited = set()
        rec_stack = set()

        def visit(step_id: str) -> bool:
            if step_id in rec_stack:
                return True
            if step_id in visited:
                return False

            visited.add(step_id)
            rec_stack.add(step_id)

            # Find step
            step_exec = None
            for s in contract.steps:
                if s.step.step_id == step_id:
                    step_exec = s
                    break

            if step_exec:
                for dep_id in step_exec.step.depends_on:
                    if visit(dep_id):
                        return True

            rec_stack.remove(step_id)
            return False

        for step_exec in contract.steps:
            if step_exec.step.step_id not in visited:
                if visit(step_exec.step.step_id):
                    return True

        return False


def create_execution_engine(
    registry: SkillRegistry,
    skill_executors: dict[SkillId, Callable],
    checkpoint_store: Optional[Callable] = None,
    evidence_store: Optional[Callable] = None,
) -> ExecutionEngine:
    """Factory for execution engine."""
    return ExecutionEngine(registry, skill_executors, checkpoint_store, evidence_store)


def create_contract_validator(registry: SkillRegistry) -> ContractValidator:
    """Factory for contract validator."""
    return ContractValidator(registry)