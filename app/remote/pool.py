"""Production-safe remote worker pool.

Owns worker identity, capability/capacity routing, heartbeats,
drain mode, stale eviction, reconnect, job reassignment,
cancellation propagation, and idempotent result intake.

No sockets or cloud dependencies. All time flows through an
injectable clock so tests stay hermetic.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum

from app.remote.auth import AllowAllAuthenticator, TokenAuthenticator
from app.remote.protocol import (
    PROTOCOL_VERSION,
    AuthRejectedError,
    CancelRequest,
    DuplicateIdentityError,
    HeartbeatMessage,
    JobAssignment,
    JobResult,
    JobSpec,
    RegisterRequest,
    RegisterResponse,
    negotiate_protocol,
)
from app.remote.transport import DispatcherTransport, InMemoryTransport


class WorkerState(str, Enum):
    READY = "READY"
    DRAINING = "DRAINING"
    OFFLINE = "OFFLINE"
    UNHEALTHY = "UNHEALTHY"


@dataclass
class WorkerRecord:
    worker_id: str
    capabilities: frozenset[str]
    capacity: int
    protocol_version: int = PROTOCOL_VERSION
    used_slots: int = 0
    healthy: bool = True
    draining: bool = False
    lease_compatible: bool = True
    last_heartbeat: float = 0.0
    state: WorkerState = WorkerState.READY
    metadata: dict = field(default_factory=dict)

    @property
    def free_slots(self) -> int:
        return max(0, self.capacity - self.used_slots)

    def eligible_for(self, capability: str, needs_lease: bool) -> bool:
        if self.state == WorkerState.OFFLINE:
            return False
        if self.state == WorkerState.UNHEALTHY:
            return False
        if not self.healthy:
            return False
        if self.draining or self.state == WorkerState.DRAINING:
            return False
        if capability not in self.capabilities:
            return False
        if needs_lease and not self.lease_compatible:
            return False
        return self.free_slots > 0


@dataclass(frozen=True)
class Assignment:
    job_id: str
    worker_id: str


class RemoteWorkerPool:
    """Registry + dispatcher for local or remote execution nodes."""

    def __init__(
        self,
        transport: DispatcherTransport | None = None,
        authenticator: TokenAuthenticator | None = None,
        clock=None,
        heartbeat_timeout_seconds: float = 30.0,
    ):
        self.transport = transport or InMemoryTransport()
        self.authenticator = authenticator or AllowAllAuthenticator()
        self._clock = clock or time.monotonic
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._workers: dict[str, WorkerRecord] = {}
        self._pending: dict[str, tuple[JobSpec, str]] = {}
        self._completed: dict[str, JobResult] = {}
        self._cancelled: set[str] = set()
        self._requeue: list[JobSpec] = []
        self._job_seq = itertools.count(1)

    def now(self) -> float:
        return float(self._clock())

    def register(self, request: RegisterRequest) -> RegisterResponse:
        negotiated = negotiate_protocol(request.protocol_version)
        if not request.worker_id:
            raise ValueError("worker_id is required")
        if request.capacity < 1:
            raise ValueError("capacity must be >= 1")
        if not request.capabilities:
            raise ValueError("at least one capability is required")
        if not self.authenticator.verify(request.auth_token, request.worker_id):
            raise AuthRejectedError(f"auth rejected for {request.worker_id!r}")

        existing = self._workers.get(request.worker_id)
        if existing is not None and existing.state != WorkerState.OFFLINE:
            if not self._is_stale(existing):
                raise DuplicateIdentityError(
                    f"worker {request.worker_id!r} already registered"
                )

        reconnected = existing is not None
        record = WorkerRecord(
            worker_id=request.worker_id,
            capabilities=frozenset(request.capabilities),
            capacity=request.capacity,
            protocol_version=negotiated,
            used_slots=0,
            healthy=True,
            draining=False,
            lease_compatible=bool(request.metadata.get("lease_compatible", True)),
            last_heartbeat=self.now(),
            state=WorkerState.READY,
            metadata=dict(request.metadata),
        )
        self._workers[request.worker_id] = record
        return RegisterResponse(
            accepted=True,
            worker_id=request.worker_id,
            negotiated_version=negotiated,
            reason="reconnected" if reconnected else "registered",
        )

    def heartbeat(self, message: HeartbeatMessage) -> bool:
        record = self._workers.get(message.worker_id)
        if record is None or record.state == WorkerState.OFFLINE:
            return False
        record.last_heartbeat = self.now()
        record.used_slots = max(0, min(record.capacity, message.used_slots))
        record.healthy = bool(message.healthy)
        if not record.healthy:
            record.state = WorkerState.UNHEALTHY
        elif record.draining:
            record.state = WorkerState.DRAINING
        else:
            record.state = WorkerState.READY
        return True

    def get(self, worker_id: str) -> WorkerRecord | None:
        return self._workers.get(worker_id)

    def list_workers(self) -> list[WorkerRecord]:
        return list(self._workers.values())

    def set_drain(self, worker_id: str, draining: bool) -> bool:
        record = self._workers.get(worker_id)
        if record is None:
            return False
        record.draining = draining
        if record.state != WorkerState.OFFLINE and record.healthy:
            record.state = WorkerState.DRAINING if draining else WorkerState.READY
        return True

    def set_healthy(self, worker_id: str, healthy: bool) -> bool:
        record = self._workers.get(worker_id)
        if record is None:
            return False
        record.healthy = healthy
        if not healthy:
            record.state = WorkerState.UNHEALTHY
        elif record.state == WorkerState.UNHEALTHY:
            record.state = WorkerState.DRAINING if record.draining else WorkerState.READY
        return True

    def new_job_id(self, prefix: str = "job") -> str:
        return f"{prefix}_{next(self._job_seq):06d}"

    def dispatch(self, spec: JobSpec) -> Assignment | None:
        needs_lease = bool(spec.repo_key)
        candidates = [
            w
            for w in self._workers.values()
            if w.eligible_for(spec.capability, needs_lease)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda w: (w.used_slots, w.worker_id))
        chosen = candidates[0]
        chosen.used_slots += 1
        self._pending[spec.job_id] = (spec, chosen.worker_id)
        self.transport.send_assignment(
            JobAssignment(job_id=spec.job_id, worker_id=chosen.worker_id, spec=spec)
        )
        return Assignment(job_id=spec.job_id, worker_id=chosen.worker_id)

    def submit_result(self, result: JobResult) -> str:
        """Accept a worker result. Returns accepted|duplicate|unknown."""
        if result.job_id in self._completed:
            return "duplicate"
        pending = self._pending.get(result.job_id)
        if pending is None:
            return "unknown"
        _, worker_id = pending
        del self._pending[result.job_id]
        self._completed[result.job_id] = result
        record = self._workers.get(worker_id)
        if record is not None:
            record.used_slots = max(0, record.used_slots - 1)
        return "accepted"

    def result_for(self, job_id: str) -> JobResult | None:
        return self._completed.get(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled

    def cancel_job(self, job_id: str, reason: str = "cancelled") -> bool:
        pending = self._pending.get(job_id)
        if pending is None:
            return False
        self._cancelled.add(job_id)
        self.transport.send_cancel(CancelRequest(job_id=job_id, reason=reason))
        return True

    def disconnect(self, worker_id: str) -> list[JobSpec]:
        record = self._workers.get(worker_id)
        if record is None:
            return []
        record.state = WorkerState.OFFLINE
        return self._requeue_worker_jobs(worker_id)

    def _is_stale(self, record: WorkerRecord) -> bool:
        return (self.now() - record.last_heartbeat) > self.heartbeat_timeout_seconds

    def evict_stale(self) -> list[str]:
        evicted: list[str] = []
        for worker_id, record in list(self._workers.items()):
            if record.state == WorkerState.OFFLINE:
                continue
            if self._is_stale(record):
                record.state = WorkerState.OFFLINE
                evicted.append(worker_id)
                self._requeue_worker_jobs(worker_id)
        return evicted

    def _requeue_worker_jobs(self, worker_id: str) -> list[JobSpec]:
        orphaned: list[JobSpec] = []
        for job_id, (spec, owner) in list(self._pending.items()):
            if owner == worker_id:
                del self._pending[job_id]
                orphaned.append(spec)
                self._requeue.append(spec)
        record = self._workers.get(worker_id)
        if record is not None:
            record.used_slots = 0
        return orphaned

    def take_requeue(self) -> list[JobSpec]:
        due = list(self._requeue)
        self._requeue.clear()
        return due

    def pending_jobs(self) -> list[JobSpec]:
        return [spec for spec, _ in self._pending.values()]
