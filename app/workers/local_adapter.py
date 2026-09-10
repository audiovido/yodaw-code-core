"""Local worker adapter: run in-process workers through the pool."""

from __future__ import annotations

import inspect

from app.remote.pool import RemoteWorkerPool
from app.remote.protocol import (
    HeartbeatMessage,
    JobAssignment,
    JobResult,
    RegisterRequest,
)
from app.workers.base import Worker


class LocalWorkerAdapter:
    """Register a local Worker as a pool node with remote-equivalent semantics.

    Assignments arriving over the pool transport execute synchronously
    against the wrapped worker; results return through the idempotent
    pool intake so local and remote execution share one result path.
    """

    def __init__(
        self,
        worker: Worker,
        pool: RemoteWorkerPool,
        worker_id: str | None = None,
        capacity: int = 1,
    ):
        self.worker = worker
        self.pool = pool
        self.worker_id = worker_id or f"local-{worker.name}"
        self.capacity = capacity
        pool.register(
            RegisterRequest(
                worker_id=self.worker_id,
                capabilities=frozenset(set(worker.capabilities)),
                capacity=capacity,
                auth_token="",
                metadata={"lease_compatible": True, "local": True},
            )
        )
        transport = pool.transport
        if hasattr(transport, "attach_worker"):
            transport.attach_worker(self.worker_id, self._on_assignment)

    def _takes_metadata(self) -> bool:
        try:
            params = inspect.signature(self.worker.execute).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            p.name == "metadata" or p.kind == p.VAR_KEYWORD for p in params
        )

    def _on_assignment(self, assignment: JobAssignment) -> None:
        if assignment.worker_id != self.worker_id:
            return
        spec = assignment.spec
        if self.pool.is_cancelled(spec.job_id):
            self.pool.submit_result(
                JobResult(
                    job_id=spec.job_id,
                    worker_id=self.worker_id,
                    success=False,
                    output={"goal": spec.goal},
                    evidence=[],
                    error={"type": "Cancelled", "message": "cancelled before start"},
                )
            )
            return
        try:
            if self._takes_metadata():
                raw = self.worker.execute(spec.goal, dict(spec.metadata))
            else:
                raw = self.worker.execute(spec.goal)
        except Exception as exc:
            self.pool.submit_result(
                JobResult(
                    job_id=spec.job_id,
                    worker_id=self.worker_id,
                    success=False,
                    output={"goal": spec.goal},
                    evidence=[],
                    error={"type": type(exc).__name__, "message": str(exc)},
                )
            )
            return
        if isinstance(raw, JobResult):
            result = raw
        else:
            success = bool(raw.get("success", False))
            result = JobResult(
                job_id=spec.job_id,
                worker_id=self.worker_id,
                success=success,
                output=dict(raw.get("output", {})),
                evidence=list(raw.get("evidence", [])),
                error=raw.get("error"),
            )
        self.pool.submit_result(result)

    def heartbeat(self) -> bool:
        record = self.pool.get(self.worker_id)
        used = record.used_slots if record else 0
        return self.pool.heartbeat(
            HeartbeatMessage(worker_id=self.worker_id, used_slots=used, healthy=True)
        )

    def set_drain(self, draining: bool) -> bool:
        return self.pool.set_drain(self.worker_id, draining)
