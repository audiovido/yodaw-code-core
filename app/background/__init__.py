"""Kodgar background coding system.

The product architecture in one package:

    React UI / CLI
         |
    Task API (app.background.api)      submit returns immediately
         |
    Grok Architect (app.background.planner)   decides what and who
         |
    Executor registry (app.background.executors)  real CLIs only
         |
    Background engine (app.background.engine)  durable, cancellable
         |
    Isolated git worktree -> real edits -> tests -> build
         |
    Verifier (app.background.verifier)   the only authority
         |
    Commit + live event stream (app.background.events) -> UI
"""

from app.background.engine import TaskEngine
from app.background.events import TaskEventBus, get_bus
from app.background.models import (
    PlanResult,
    PlannerError,
    TaskEvent,
    TaskRecord,
    TaskRequest,
    TaskState,
    VerificationReport,
)
from app.background.planner import GrokArchitect
from app.background.store import TaskStore
from app.background.verifier import TaskVerifier

__all__ = [
    "GrokArchitect",
    "PlanResult",
    "PlannerError",
    "TaskEngine",
    "TaskEvent",
    "TaskEventBus",
    "TaskRecord",
    "TaskRequest",
    "TaskState",
    "TaskStore",
    "TaskVerifier",
    "VerificationReport",
    "get_bus",
]
