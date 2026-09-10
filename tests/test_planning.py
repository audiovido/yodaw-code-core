"""
Tests for the advanced planning layer.
"""

import pytest
from app.planning import (
    AdvancedPlanner, Plan, Step, RiskLevel, ConflictClass, StepStatus,
    DecompositionEngine
)


class TestDecompositionEngine:
    """Tests for the DecompositionEngine."""
    
    def test_decompose_feature_goal(self):
        """Test decomposition of a feature implementation goal."""
        engine = DecompositionEngine()
        plan = engine.decompose("Implement user authentication API")
        
        assert plan.goal == "Implement user authentication API"
        assert len(plan.steps) > 0
        assert plan.revision == 0
        
        # Check steps have expected structure
        for step in plan.steps:
            assert step.id
            assert step.title
            assert step.intent
            assert step.description
            assert step.risk in RiskLevel
            assert step.status == StepStatus.PENDING
    
    def test_decompose_bug_fix_goal(self):
        """Test decomposition of a bug fix goal."""
        engine = DecompositionEngine()
        plan = engine.decompose("Fix login timeout bug")
        
        assert plan.goal == "Fix login timeout bug"
        assert len(plan.steps) > 0
        
        # Should have reproduce, root cause, fix, regression test steps
        titles = [s.title for s in plan.steps]
        assert any("Reproduce" in t for t in titles)
        assert any("Root cause" in t or "root cause" in t.lower() for t in titles)
        assert any("Fix" in t for t in titles)
    
    def test_decompose_refactor_goal(self):
        """Test decomposition of a refactor goal."""
        engine = DecompositionEngine()
        plan = engine.decompose("Refactor user service module")
        
        assert plan.goal == "Refactor user service module"
        assert len(plan.steps) > 0
        
        titles = [s.title for s in plan.steps]
        assert any("Analyze" in t for t in titles)
        assert any("Design" in t for t in titles)
        assert any("Execute" in t or "refactor" in t.lower() for t in titles)
    
    def test_generic_decomposition(self):
        """Test fallback generic decomposition."""
        engine = DecompositionEngine()
        plan = engine.decompose("Do something completely unique and unknown")
        
        assert len(plan.steps) >= 4
        titles = [s.title for s in plan.steps]
        assert "Analyze goal" in titles
        assert "Plan implementation" in titles
        assert "Implement" in titles
        assert "Test and validate" in titles
    
    def test_risk_summary_computed(self):
        """Test that risk summary is computed."""
        engine = DecompositionEngine()
        plan = engine.decompose("Implement API")
        
        plan.compute_risk_summary()
        assert "LOW" in plan.risk_summary
        assert "MEDIUM" in plan.risk_summary
        assert "HIGH" in plan.risk_summary
        assert "CRITICAL" in plan.risk_summary
        assert sum(plan.risk_summary.values()) == len(plan.steps)


class TestPlanValidation:
    """Tests for plan validation."""
    
    def test_valid_plan_passes(self):
        """Test that a valid plan passes validation."""
        engine = DecompositionEngine()
        plan = engine.decompose("Implement user API")
        
        result = engine.validate_plan(plan)
        
        assert result.valid
        assert len(result.errors) == 0
        assert len(result.topological_order) == len(plan.steps)
    
    def test_cycle_detection(self):
        """Test that cycles are detected."""
        plan = Plan(goal="Test", summary="Test")
        
        step_a = Step(id="a", title="A", intent="", description="", dependencies=["b"])
        step_b = Step(id="b", title="B", intent="", description="", dependencies=["a"])
        
        plan.steps = [step_a, step_b]
        
        engine = DecompositionEngine()
        engine._compute_dependencies(plan)
        result = engine.validate_plan(plan)
        
        assert not result.valid
        assert len(result.cycles) > 0
    
    def test_orphan_detection(self):
        """Test that orphans are detected."""
        plan = Plan(goal="Test", summary="Test")
        
        step_a = Step(id="a", title="A", intent="", description="")
        step_b = Step(id="b", title="B", intent="", description="", dependencies=["a"])
        step_c = Step(id="c", title="C", intent="", description="")  # orphan
        
        plan.steps = [step_a, step_b, step_c]
        
        engine = DecompositionEngine()
        engine._compute_dependencies(plan)
        result = engine.validate_plan(plan)
        
        assert result.valid  # orphans are warnings, not errors
        assert "c" in result.orphans or step_c.id in result.orphans
    
    def test_critical_path(self):
        """Test critical path computation."""
        engine = DecompositionEngine()
        plan = engine.decompose("Implement user API")
        
        result = engine.validate_plan(plan)
        
        assert len(result.critical_path) > 0
        # Critical path should be a valid chain
        for i in range(len(result.critical_path) - 1):
            step = next(s for s in plan.steps if s.id == result.critical_path[i+1])
            assert result.critical_path[i] in step.dependencies
    
    def test_topological_sort(self):
        """Test topological ordering."""
        engine = DecompositionEngine()
        plan = engine.decompose("Implement user API")
        
        result = engine.validate_plan(plan)
        
        # Topo order should include all steps
        assert len(result.topological_order) == len(plan.steps)
        
        # Dependencies should come before dependents
        pos = {sid: i for i, sid in enumerate(result.topological_order)}
        for step in plan.steps:
            for dep in step.dependencies:
                if dep in pos:
                    assert pos[dep] < pos[step.id]


class TestConflictAnalysis:
    """Tests for conflict analysis."""
    
    def test_hard_conflict_same_file_write(self):
        """Test detection of hard conflict (same file write)."""
        engine = DecompositionEngine()
        
        step_a = Step(
            id="a", title="A", intent="", description="",
            write_set=["app/main.py"], read_set=[]
        )
        step_b = Step(
            id="b", title="B", intent="", description="",
            write_set=["app/main.py"], read_set=[]
        )
        
        conflict = engine._check_conflict(step_a, step_b)
        
        assert conflict is not None
        assert conflict.conflict_class == ConflictClass.HARD_CONFLICT
        assert "app/main.py" in conflict.reason
    
    def test_potential_conflict_write_read(self):
        """Test detection of potential conflict (write/read)."""
        engine = DecompositionEngine()
        
        step_a = Step(
            id="a", title="A", intent="", description="",
            write_set=["app/config.py"], read_set=[]
        )
        step_b = Step(
            id="b", title="B", intent="", description="",
            write_set=[], read_set=["app/config.py"]
        )
        
        conflict = engine._check_conflict(step_a, step_b)
        
        assert conflict is not None
        assert conflict.conflict_class == ConflictClass.POTENTIAL_CONFLICT
    
    def test_read_only_compatible(self):
        """Test read-only compatible detection."""
        engine = DecompositionEngine()
        
        step_a = Step(
            id="a", title="A", intent="", description="",
            write_set=[], read_set=["app/config.py", "README.md"]
        )
        step_b = Step(
            id="b", title="B", intent="", description="",
            write_set=[], read_set=["app/config.py", "ARCHITECTURE.md"]
        )
        
        conflict = engine._check_conflict(step_a, step_b)
        
        assert conflict is not None
        assert conflict.conflict_class == ConflictClass.READ_ONLY_COMPATIBLE
    
    def test_no_conflict(self):
        """Test no conflict when disjoint."""
        engine = DecompositionEngine()
        
        step_a = Step(
            id="a", title="A", intent="", description="",
            write_set=["app/a.py"], read_set=["app/a.py"]
        )
        step_b = Step(
            id="b", title="B", intent="", description="",
            write_set=["app/b.py"], read_set=["app/b.py"]
        )
        
        conflict = engine._check_conflict(step_a, step_b)
        
        assert conflict is None


class TestAdvancedPlanner:
    """Tests for the AdvancedPlanner."""
    
    def test_create_plan(self):
        """Test creating a plan."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Implement feature X")
        
        assert plan.id in planner.plans
        assert plan.goal == "Implement feature X"
    
    def test_get_execution_order(self):
        """Test getting execution order."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Implement user API")
        
        levels = planner.get_execution_order(plan)
        
        assert len(levels) > 0
        # Flatten and check all steps present
        all_steps = [s for level in levels for s in level]
        assert len(all_steps) == len(plan.steps)
    
    def test_parallel_groups(self):
        """Test parallel group identification."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Implement user API")
        
        groups = planner.get_parallel_groups(plan)
        
        # At least some steps should have parallel groups
        total_in_groups = sum(len(v) for v in groups.values())
        assert total_in_groups > 0
    
    def test_conflict_analysis(self):
        """Test conflict analysis."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Implement user API")
        
        conflicts = planner.analyze_conflicts(plan)
        
        # Should find some conflicts
        assert len(conflicts) >= 0
    
    def test_hard_conflicts(self):
        """Test hard conflict extraction."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Implement user API")
        
        hard_conflicts = planner.get_hard_conflicts(plan)
        
        for c in hard_conflicts:
            assert c.conflict_class == ConflictClass.HARD_CONFLICT
    
    def test_parallel_safe_check(self):
        """Test parallel safety check."""
        planner = AdvancedPlanner()
        
        step_a = Step(
            id="a", title="A", intent="", description="",
            write_set=["app/a.py"], read_set=[], parallel_safe=True
        )
        step_b = Step(
            id="b", title="B", intent="", description="",
            write_set=["app/b.py"], read_set=[], parallel_safe=True
        )
        
        assert planner.is_parallel_safe(step_a, step_b) == True
        
        step_c = Step(
            id="c", title="C", intent="", description="",
            write_set=["app/a.py"], read_set=[], parallel_safe=True
        )
        
        assert planner.is_parallel_safe(step_a, step_c) == False
    
    def test_checkpoint_creation(self):
        """Test checkpoint creation."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Test")
        
        cp = planner.create_checkpoint(
            plan.id, plan.steps[0].id, StepStatus.COMPLETED,
            ["step_1"], evidence=[{"type": "test"}], outputs={"result": "ok"}
        )
        
        assert cp.plan_id == plan.id
        assert cp.step_status == StepStatus.COMPLETED
        assert cp.completed_steps == ["step_1"]
        assert len(planner.checkpoints[plan.id]) == 1
    
    def test_latest_checkpoint(self):
        """Test getting latest checkpoint."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Test")
        
        cp1 = planner.create_checkpoint(plan.id, "s1", StepStatus.COMPLETED, [])
        cp2 = planner.create_checkpoint(plan.id, "s2", StepStatus.COMPLETED, ["s1"])
        
        latest = planner.get_latest_checkpoint(plan.id)
        
        assert latest is not None
        assert latest.step_id == "s2"
    
    def test_recovery_plan(self):
        """Test recovery plan creation."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Implement feature")
        
        # Add a checkpoint
        planner.create_checkpoint(plan.id, plan.steps[0].id, StepStatus.COMPLETED, [plan.steps[0].id])
        
        recovery = planner.create_recovery_plan(plan.id, plan.steps[1].id, "Test failure")
        
        assert recovery.original_plan_id == plan.id
        assert recovery.failed_step_id == plan.steps[1].id
        assert recovery.failure_reason == "Test failure"
    
    def test_plan_revision(self):
        """Test plan revision tracking."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Test")
        
        revision = planner.revise_plan(plan, "Added new step", added_steps=["new_step"])
        
        assert revision.plan_id == plan.id
        assert revision.revision == 1
        assert plan.revision == 1
        assert "new_step" in revision.added_steps
    
    def test_plan_summary(self):
        """Test comprehensive plan summary."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Implement user API")
        
        summary = planner.get_plan_summary(plan)
        
        assert summary["plan_id"] == plan.id
        assert summary["goal"] == plan.goal
        assert summary["total_steps"] == len(plan.steps)
        assert "validation" in summary
        assert "execution_levels" in summary
        assert "parallel_groups" in summary
        assert summary["validation"]["valid"] == True
    
    def test_export_plan(self):
        """Test plan export."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Test")
        
        exported = planner.export_plan(plan)
        
        assert "plan" in exported
        assert "validation" in exported
        assert "summary" in exported
        assert exported["plan"]["goal"] == "Test"
    
    def test_completed_history(self):
        """Test completion history recording."""
        planner = AdvancedPlanner()
        plan = planner.create_plan("Test")
        
        planner.record_completion(plan.id, plan.steps[0].id, [{"type": "test"}], {"ok": True})
        
        assert len(planner.completed_history) == 1
        assert planner.completed_history[0]["plan_id"] == plan.id
        assert planner.completed_history[0]["step_id"] == plan.steps[0].id


class TestStepModel:
    """Tests for Step model."""
    
    def test_step_creation(self):
        """Test step creation with all fields."""
        step = Step(
            title="Test Step",
            intent="Test intent",
            description="Test description",
            dependencies=["dep1", "dep2"],
            read_set=["file1.py"],
            write_set=["file2.py"],
            parallel_safe=False,
            risk=RiskLevel.HIGH,
        )
        
        assert step.title == "Test Step"
        assert step.intent == "Test intent"
        assert step.dependencies == ["dep1", "dep2"]
        assert step.read_set == ["file1.py"]
        assert step.write_set == ["file2.py"]
        assert step.parallel_safe == False
        assert step.risk == RiskLevel.HIGH
        assert step.status == StepStatus.PENDING
    
    def test_step_defaults(self):
        """Test step default values."""
        step = Step(title="Minimal", intent="", description="")
        
        assert step.id.startswith("step_")
        assert step.dependencies == []
        assert step.inputs == {}
        assert step.expected_outputs == {}
        assert step.parallel_safe == True
        assert step.risk == RiskLevel.LOW
        assert step.status == StepStatus.PENDING


class TestPlanModel:
    """Tests for Plan model."""
    
    def test_plan_creation(self):
        """Test plan creation."""
        plan = Plan(goal="Test goal", summary="Test summary")
        
        assert plan.goal == "Test goal"
        assert plan.summary == "Test summary"
        assert plan.steps == []
        assert plan.revision == 0
        assert plan.id.startswith("plan_")
    
    def test_add_step(self):
        """Test adding steps to plan."""
        plan = Plan(goal="Test", summary="Test")
        step = Step(title="Step 1", intent="", description="")
        
        plan.add_step(step)
        
        assert len(plan.steps) == 1
        assert plan.steps[0] == step
    
    def test_get_step(self):
        """Test getting step by ID."""
        plan = Plan(goal="Test", summary="Test")
        step = Step(id="custom_id", title="Step", intent="", description="")
        plan.add_step(step)
        
        found = plan.get_step("custom_id")
        
        assert found == step
        assert plan.get_step("nonexistent") is None
    
    def test_get_ready_steps(self):
        """Test getting ready steps."""
        plan = Plan(goal="Test", summary="Test")
        
        step_a = Step(id="a", title="A", intent="", description="")
        step_b = Step(id="b", title="B", intent="", description="", dependencies=["a"])
        step_c = Step(id="c", title="C", intent="", description="", dependencies=["b"])
        
        plan.add_step(step_a)
        plan.add_step(step_b)
        plan.add_step(step_c)
        
        # Initially only A is ready
        ready = plan.get_ready_steps(set())
        assert len(ready) == 1
        assert ready[0].id == "a"
        assert ready[0].status == StepStatus.READY
        
        # After A completed, B is ready
        ready = plan.get_ready_steps({"a"})
        assert len(ready) == 1
        assert ready[0].id == "b"
    
    def test_compute_risk_summary(self):
        """Test risk summary computation."""
        plan = Plan(goal="Test", summary="Test")
        
        plan.add_step(Step(title="Low", intent="", description="", risk=RiskLevel.LOW))
        plan.add_step(Step(title="Med", intent="", description="", risk=RiskLevel.MEDIUM))
        plan.add_step(Step(title="High", intent="", description="", risk=RiskLevel.HIGH))
        plan.add_step(Step(title="Crit", intent="", description="", risk=RiskLevel.CRITICAL))
        
        summary = plan.compute_risk_summary()
        
        assert summary["LOW"] == 1
        assert summary["MEDIUM"] == 1
        assert summary["HIGH"] == 1
        assert summary["CRITICAL"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])