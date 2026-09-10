"""Remote-capable worker registry: local workers + pooled nodes."""

from __future__ import annotations

from app.remote.dispatcher import RemoteDispatcher
from app.remote.pool import RemoteWorkerPool
from app.workers.local_adapter import LocalWorkerAdapter
from app.workers.registry import WorkerRegistry


class RemoteWorkerRegistry(WorkerRegistry):
    """WorkerRegistry extended with pool dispatch.

    Local lookup keeps the existing find()/status() behavior;
    dispatch_remote() routes a goal to a pooled node and returns
    the pool assignment (or None when no node is eligible).
    """

    def __init__(self, pool: RemoteWorkerPool | None = None):
        super().__init__()
        self.pool = pool or RemoteWorkerPool()
        self.dispatcher = RemoteDispatcher(self.pool)
        self.adapters: list[LocalWorkerAdapter] = []

    def attach_local(self, worker=None, worker_id: str | None = None) -> LocalWorkerAdapter:
        target = worker if worker is not None else (self.workers[0] if self.workers else None)
        if target is None:
            raise ValueError("no local worker available to attach")
        adapter = LocalWorkerAdapter(target, self.pool, worker_id=worker_id)
        self.adapters.append(adapter)
        return adapter

    def dispatch_remote(
        self,
        goal: str,
        capability: str,
        metadata: dict | None = None,
        repo_key: str = "",
        job_id: str | None = None,
    ):
        return self.dispatcher.submit(
            goal, capability, metadata=metadata, repo_key=repo_key, job_id=job_id
        )

    def remote_status(self):
        return [
            {
                "worker_id": record.worker_id,
                "state": record.state.value,
                "capabilities": sorted(record.capabilities),
                "capacity": record.capacity,
                "used_slots": record.used_slots,
                "draining": record.draining,
                "lease_compatible": record.lease_compatible,
                "protocol_version": record.protocol_version,
            }
            for record in self.pool.list_workers()
        ]
