"""Durable long-running task supervisor (Worker N).

Isolated package: owns the durable job model, worker leases with
heartbeats, stage checkpoints, crash recovery, pause/resume,
monotonic cancellation, bounded retry/backoff, structured events,
and safe shutdown. Reuses the hardened SQLite connection policy
from app.storage.db; touches no existing runtime files.
"""

from app.supervisor.models import (
    TERMINAL_STATES,
    Checkpoint,
    JobRecord,
    JobState,
    Stage,
    SupervisorEvent,
    new_job_id,
)

__all__ = [
    "TERMINAL_STATES",
    "Checkpoint",
    "JobRecord",
    "JobState",
    "Stage",
    "SupervisorEvent",
    "new_job_id",
]
