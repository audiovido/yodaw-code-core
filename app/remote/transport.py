"""Remote worker transport contract. No real network here."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from app.remote.protocol import CancelRequest, JobAssignment


class DispatcherTransport(ABC):
    """Remote dispatch interface: deliver assignments and cancels."""

    @abstractmethod
    def send_assignment(self, assignment: JobAssignment) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_cancel(self, cancel: CancelRequest) -> None:
        raise NotImplementedError


class InMemoryTransport(DispatcherTransport):
    """Synchronous fan-out to attached in-process handlers."""

    def __init__(self):
        self.sent_assignments: list[JobAssignment] = []
        self.sent_cancels: list[CancelRequest] = []
        self._handlers: dict[str, Callable[[JobAssignment], None]] = {}
        self._cancel_handlers: dict[str, list[Callable[[CancelRequest], None]]] = {}

    def attach_worker(
        self, worker_id: str, handler: Callable[[JobAssignment], None]
    ) -> None:
        self._handlers[worker_id] = handler

    def detach_worker(self, worker_id: str) -> None:
        self._handlers.pop(worker_id, None)
        self._cancel_handlers.pop(worker_id, None)

    def on_cancel(
        self, worker_id: str, handler: Callable[[CancelRequest], None]
    ) -> None:
        self._cancel_handlers.setdefault(worker_id, []).append(handler)

    def send_assignment(self, assignment: JobAssignment) -> None:
        self.sent_assignments.append(assignment)
        handler = self._handlers.get(assignment.worker_id)
        if handler is not None:
            handler(assignment)

    def send_cancel(self, cancel: CancelRequest) -> None:
        self.sent_cancels.append(cancel)
        for assignment in self.sent_assignments:
            if assignment.job_id != cancel.job_id:
                continue
            for handler in self._cancel_handlers.get(assignment.worker_id, []):
                handler(cancel)
