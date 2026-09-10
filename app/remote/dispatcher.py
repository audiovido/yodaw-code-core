"""High-level remote dispatch interface over a RemoteWorkerPool."""

from __future__ import annotations

from app.remote.pool import Assignment, RemoteWorkerPool
from app.remote.protocol import JobResult, JobSpec


class RemoteDispatcher:
    """Submit goals, read results, cancel, and reassign orphaned jobs."""

    def __init__(self, pool: RemoteWorkerPool):
        self.pool = pool

    def submit(
        self,
        goal: str,
        capability: str,
        metadata: dict | None = None,
        repo_key: str = "",
        job_id: str | None = None,
    ) -> Assignment | None:
        spec = JobSpec(
            job_id=job_id or self.pool.new_job_id(),
            goal=goal,
            capability=capability,
            metadata=dict(metadata or {}),
            repo_key=repo_key,
        )
        return self.pool.dispatch(spec)

    def result(self, job_id: str) -> JobResult | None:
        return self.pool.result_for(job_id)

    def cancel(self, job_id: str, reason: str = "cancelled") -> bool:
        return self.pool.cancel_job(job_id, reason=reason)

    def reassign_orphans(self) -> list[Assignment]:
        assignments: list[Assignment] = []
        for spec in self.pool.take_requeue():
            assignment = self.pool.dispatch(spec)
            if assignment is not None:
                assignments.append(assignment)
            else:
                self.pool._requeue.append(spec)
        return assignments

    def pending(self):
        return self.pool.pending_jobs()
