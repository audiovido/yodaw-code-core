"""
Tests for Execution Contracts and Multi-step Execution.
"""

import pytest

from app.skills.models import (
    Intent, Skill, SkillId, SkillInput, SkillPrerequisite,
    PlanningGuidance, ValidationGuidance, RiskLevel, EvidenceSchema,
    DecompositionStep, DecompositionResult, ClassificationResult, SkillEvidence
)
from app.skills.registry import SkillRegistry
from app.skills.execution import (
    ExecutionEngine, ExecutionContract, StepExecution,
    ExecutionState, StepState, ContractValidator, ExecutionCheckpoint
)
from app.skills.skills import BugfixSkill, TestSkill


@pytest.fixture
def registry():
    """Create a registry with test skills."""
    reg = SkillRegistry()
    for skill_class in [BugfixSkill, TestSkill]:
        skill = skill_class().create_skill()
        reg.register(skill)
    return reg


@pytest.fixture
def sample_decomposition():
    """Create a sample decomposition."""
    steps = [
        DecompositionStep(
            step_id="step_1_reproduce",
            skill_id="test",
            description="Reproduce the bug",
            inputs={"task": "reproduce"},
            depends_on=[],
            estimated_duration_minutes=30,
            requires_approval=False,
        ),
        DecompositionStep(
            step_id="step_2_diagnose",
            skill_id="bugfix",
            description="Diagnose root cause",
            inputs={"task": "diagnose"},
            depends_on=["step_1_reproduce"],
            estimated_duration_minutes=45,
            requires_approval=False,
        ),
        DecompositionStep(
            step_id="step_3_fix",
            skill_id="bugfix",
            description="Implement minimal fix",
            inputs={"task": "fix"},
            depends_on=["step_2_diagnose"],
            estimated_duration_minutes=30,
            requires_approval=True,
            rollback_step_id="step_2_diagnose",
        ),
        DecompositionStep(
            step_id="step_4_test",
            skill_id="test",
            description="Run tests",
            inputs={"task": "test"},
            depends_on=["step_3_fix"],
            estimated_duration_minutes=30,
            requires_approval=False,
        ),
    ]

    return DecompositionResult(
        steps=steps,
        total_estimated_minutes=135,
        requires_human_review=True,
        risk_level=RiskLevel.MEDIUM,
        metadata={"intent": "bugfix"},
    )


@pytest.fixture
def skill_executors():
    """Create mock skill executors."""
    def bugfix_executor(inputs, context):
        return SkillEvidence(
            skill_id="bugfix",
            execution_id=context.get("execution_id", "test"),
            timestamp="2024-01-01T10:00:00Z",
            success=True,
            inputs=inputs,
            outputs={"fix_description": "Fixed null pointer"},
            artifacts={},
            metrics={"duration_minutes": 30.0},
            errors=[],
            metadata={},
        )

    def test_executor(inputs, context):
        return SkillEvidence(
            skill_id="test",
            execution_id=context.get("execution_id", "test"),
            timestamp="2024-01-01T10:00:00Z",
            success=True,
            inputs=inputs,
            outputs={"test_results": "passed"},
            artifacts={},
            metrics={"duration_minutes": 15.0},
            errors=[],
            metadata={},
        )

    return {
        "bugfix": bugfix_executor,
        "test": test_executor,
    }


class TestExecutionContract:
    """Tests for ExecutionContract."""

    def test_contract_creation(self, sample_decomposition):
        """Test creating an execution contract."""
        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        assert contract.execution_id == "test-exec-1"
        assert len(contract.steps) == 4
        assert contract.state == ExecutionState.PENDING
        assert contract.current_step_index == 0

    def test_get_current_step(self, sample_decomposition):
        """Test getting current step."""
        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        current = contract.get_current_step()
        assert current is not None
        assert current.step.step_id == "step_1_reproduce"

    def test_get_completed_steps(self, sample_decomposition):
        """Test getting completed steps."""
        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        # Mark first step as completed
        contract.steps[0].state = StepState.COMPLETED

        completed = contract.get_completed_steps()
        assert len(completed) == 1
        assert completed[0].step.step_id == "step_1_reproduce"

    def test_is_complete(self, sample_decomposition):
        """Test checking if execution is complete."""
        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        assert contract.is_complete() is False

        for step in contract.steps:
            step.state = StepState.COMPLETED

        assert contract.is_complete() is True

    def test_has_failures(self, sample_decomposition):
        """Test checking for failures."""
        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        assert contract.has_failures() is False

        contract.steps[0].state = StepState.FAILED
        assert contract.has_failures() is True

    def test_can_proceed(self, sample_decomposition):
        """Test checking if can proceed to next step."""
        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        # Can't proceed - current step not complete
        assert contract.can_proceed() is False

        # Complete first step
        contract.steps[0].state = StepState.COMPLETED
        assert contract.can_proceed() is True


class TestExecutionEngine:
    """Tests for ExecutionEngine."""

    def test_create_contract(self, registry, skill_executors, sample_decomposition):
        """Test creating a contract via engine."""
        engine = ExecutionEngine(registry, skill_executors)
        contract = engine.create_contract(sample_decomposition)

        assert contract.execution_id is not None
        assert contract.decomposition == sample_decomposition

    def test_execute_success(self, registry, skill_executors, sample_decomposition):
        """Test successful execution."""
        engine = ExecutionEngine(registry, skill_executors)
        contract = engine.create_contract(sample_decomposition)

        # Execute with auto_approve and test_mode
        context = {
            "auto_approve": True,
            "test_mode": True,
            "execution_id": contract.execution_id,
        }

        result = engine.execute(contract, context)

        assert result.state == ExecutionState.COMPLETED
        assert all(s.state == StepState.COMPLETED for s in result.steps)

    def test_execute_with_progress_callback(self, registry, skill_executors, sample_decomposition):
        """Test execution with progress callback."""
        engine = ExecutionEngine(registry, skill_executors)
        contract = engine.create_contract(sample_decomposition)

        progress_calls = []

        def callback(contract, step_exec):
            progress_calls.append((contract.current_step_index, step_exec.step.step_id, step_exec.state))

        context = {
            "auto_approve": True,
            "test_mode": True,
            "execution_id": contract.execution_id,
        }

        result = engine.execute(contract, context, progress_callback=callback)

        assert result.state == ExecutionState.COMPLETED
        # Called at start (RUNNING) and end (COMPLETED) of each step = 8 calls for 4 steps
        assert len(progress_calls) == 8
        # Verify we get RUNNING then COMPLETED for each step
        for i in range(4):
            assert progress_calls[i*2][2] == StepState.RUNNING
            assert progress_calls[i*2+1][2] == StepState.COMPLETED

    def test_cancel_execution(self, registry, skill_executors, sample_decomposition):
        """Test cancelling execution."""
        engine = ExecutionEngine(registry, skill_executors)
        contract = engine.create_contract(sample_decomposition)

        context = {
            "auto_approve": True,
            "test_mode": True,
            "execution_id": contract.execution_id,
        }

        # Start execution in background and cancel
        # For simplicity, we'll test the cancel flag directly
        engine.request_cancel()

        result = engine.execute(contract, context)

        assert result.state == ExecutionState.CANCELLED
        assert result.cancelled_at is not None

    def test_pause_resume_execution(self, registry, skill_executors, sample_decomposition):
        """Test pausing and resuming execution."""
        engine = ExecutionEngine(registry, skill_executors)
        contract = engine.create_contract(sample_decomposition)

        engine.request_pause()
        assert engine._pause_requested is True

        engine.request_resume()
        assert engine._pause_requested is False


class TestContractValidator:
    """Tests for ContractValidator."""

    def test_validate_valid_contract(self, registry, sample_decomposition):
        """Test validating a valid contract."""
        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        validator = ContractValidator(registry)
        errors = validator.validate(contract)

        assert len(errors) == 0

    def test_validate_empty_decomposition(self, registry):
        """Test validation fails for empty decomposition."""
        from app.skills.decomposition import DecompositionResult
        empty_decomp = DecompositionResult(
            steps=[],
            total_estimated_minutes=0,
            requires_human_review=False,
            risk_level=RiskLevel.LOW,
        )

        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=empty_decomp,
        )

        validator = ContractValidator(registry)
        errors = validator.validate(contract)

        assert len(errors) > 0
        assert any("No steps" in e for e in errors)

    def test_validate_missing_skill(self, registry, sample_decomposition):
        """Test validation fails for missing skill."""
        # Modify step to use non-existent skill
        sample_decomposition.steps[0].skill_id = "nonexistent"

        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        validator = ContractValidator(registry)
        errors = validator.validate(contract)

        assert len(errors) > 0
        assert any("not found" in e for e in errors)

    def test_validate_missing_dependency(self, registry, sample_decomposition):
        """Test validation fails for missing dependency."""
        # Add dependency to non-existent step
        sample_decomposition.steps[1].depends_on = ["nonexistent_step"]

        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        validator = ContractValidator(registry)
        errors = validator.validate(contract)

        assert len(errors) > 0
        assert any("not found" in e for e in errors)

    def test_validate_missing_rollback_step(self, registry, sample_decomposition):
        """Test validation fails for missing rollback step."""
        sample_decomposition.steps[2].rollback_step_id = "nonexistent_rollback"

        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=sample_decomposition,
        )

        validator = ContractValidator(registry)
        errors = validator.validate(contract)

        assert len(errors) > 0
        assert any("Rollback step" in e for e in errors)

    def test_validate_circular_dependency(self, registry):
        """Test validation detects circular dependencies."""
        steps = [
            DecompositionStep(
                step_id="step_a",
                skill_id="bugfix",
                description="Step A",
                inputs={},
                depends_on=["step_b"],  # A depends on B
                estimated_duration_minutes=30,
            ),
            DecompositionStep(
                step_id="step_b",
                skill_id="test",
                description="Step B",
                inputs={},
                depends_on=["step_a"],  # B depends on A - cycle!
                estimated_duration_minutes=30,
            ),
        ]

        decomp = DecompositionResult(
            steps=steps,
            total_estimated_minutes=60,
            requires_human_review=False,
            risk_level=RiskLevel.LOW,
        )

        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=decomp,
        )

        validator = ContractValidator(registry)
        errors = validator.validate(contract)

        assert len(errors) > 0
        assert any("Circular dependency" in e for e in errors)

    def test_validate_high_risk_requires_approval(self, registry):
        """Test validation requires approval for high-risk tasks."""
        steps = [
            DecompositionStep(
                step_id="step_1",
                skill_id="migration",
                description="Migrate",
                inputs={},
                depends_on=[],
                estimated_duration_minutes=60,
                requires_approval=False,  # No approval!
            ),
        ]

        decomp = DecompositionResult(
            steps=steps,
            total_estimated_minutes=60,
            requires_human_review=False,
            risk_level=RiskLevel.HIGH,
        )

        contract = ExecutionContract(
            execution_id="test-exec-1",
            decomposition=decomp,
        )

        validator = ContractValidator(registry)
        errors = validator.validate(contract)

        assert len(errors) > 0
        assert any("approval" in e.lower() for e in errors)


class TestStepExecution:
    """Tests for StepExecution."""

    def test_step_execution_creation(self, sample_decomposition):
        """Test creating step execution."""
        step = sample_decomposition.steps[0]
        step_exec = StepExecution(step=step)

        assert step_exec.step == step
        assert step_exec.state == StepState.PENDING
        assert step_exec.start_time is None
        assert step_exec.end_time is None
        assert step_exec.evidence is None
        assert step_exec.error is None
        assert step_exec.retry_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])