"""Adaptive provider and model routing for YODAW."""

from app.routing.profiling import TaskProfile, build_task_profile
from app.routing.capabilities import ModelCapability, ModelCapabilityRegistry
from app.routing.policy import BudgetPolicy
from app.routing.selector import RoutingDecision, AdaptiveRouter
from app.routing.failover import (
    FailoverResult,
    FailureKind,
    classify_failure,
    execute_with_failover,
)
from app.routing.history import PerformanceRecord, HistoryStore

__all__ = [
    "TaskProfile",
    "build_task_profile",
    "ModelCapability",
    "ModelCapabilityRegistry",
    "BudgetPolicy",
    "RoutingDecision",
    "AdaptiveRouter",
    "FailoverResult",
    "FailureKind",
    "classify_failure",
    "execute_with_failover",
    "PerformanceRecord",
    "HistoryStore",
]
