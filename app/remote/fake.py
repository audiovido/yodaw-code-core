"""Fake remote worker for hermetic tests. No network."""

from __future__ import annotations

from collections.abc import Callable

from app.remote.protocol import CancelRequest, JobAssignment, JobResult, JobSpec
from app.remote.transport import InMemoryTransport


class FakeRemoteWorker:
    """Deterministic in-process stand-in for a remote execution node."""

    def __init__(
        self,
        worker_id: str,
        transport: InMemoryTransport,
        handler: Callable[[JobSpec], JobResult] | None = None,
    ):
        self.worker_id = worker_id
        self.transport = transport
        self.handler = handler or self._default_handler
        self.received: list[JobAssignment] = []
        self.cancels: list[CancelRequest] = []
        self.on_result: Callable[[JobResult], str] | None = None
        transport.attach_worker(worker_id, self._on_assignment)
        transport.on_cancel(worker_id, self._on_cancel)

    def _default_handler(self, spec: JobSpec) -> JobResult:
        return JobResult(
            job_id=spec.job_id,
            worker_id=self.worker_id,
            success=True,
            output={"goal": spec.goal},
            evidence=[{"type": "fake_execution", "job_id": spec.job_id}],
        )

    def _on_assignment(self, assignment: JobAssignment) -> None:
        self.received.append(assignment)
        result = self.handler(assignment.spec)
        if self.on_result is not None:
            self.on_result(result)

    def _on_cancel(self, cancel: CancelRequest) -> None:
        self.cancels.append(cancel)

    def set_handler(self, handler: Callable[[JobSpec], JobResult]) -> None:
        self.handler = handler
