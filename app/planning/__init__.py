"""
YODAW Advanced Planning Layer

This module provides structured planning capabilities:
- Bounded decomposition of goals into steps
- Dependency DAG validation with cycle detection
- Read/write set conflict analysis
- Parallel group identification
- Deterministic risk scoring
- Validation boundaries and merge strategies
- Checkpoint-based recovery
- Plan revision tracking
- Completed history preservation
"""

from app.planning.models import (
    Plan, Step, Conflict, ConflictClass, RiskLevel, StepStatus,
    ValidationResult, HandoffPackage, Checkpoint, RecoveryPlan, PlanRevision
)
from app.planning.decomposition import DecompositionEngine, BUILTIN_TEMPLATES
from app.planning.planner import AdvancedPlanner, plan

__all__ = [
    # Models
    "Plan", "Step", "Conflict", "ConflictClass", "RiskLevel", "StepStatus",
    "ValidationResult", "HandoffPackage", "Checkpoint", "RecoveryPlan", "PlanRevision",
    # Engine
    "DecompositionEngine", "BUILTIN_TEMPLATES",
    # Planner
    "AdvancedPlanner", "plan",
]

__version__ = "1.0.0"