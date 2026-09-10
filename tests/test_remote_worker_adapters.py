"""Worker R: lease compatibility, local adapter parity, health states.

Hermetic: in-memory transport, injected clock, no network.
"""

from __future__ import annotations

from app.remote import (
    HeartbeatMessage,
    InMemoryTransport,
    JobSpec,
    RegisterRequest,
    RemoteWorkerPool,
)
from app.remote.dispatcher import RemoteDispatcher
from app.workers.local_adapter import LocalWorkerAdapter
from app.workers.pool_registry import RemoteWorkerRegistry


class Clock:
    def __init__(self):
        self.t = 3000.0

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


def make_pool(**kwargs):
    clock = Clock()
    transport = InMemoryTransport()
    pool = RemoteWorkerPool(
        transport=transport, clock=clock, heartbeat_timeout_seconds=10.0, **kwargs
    )
    return pool, transport, clock


def test_lease_compatibility_gates_repo_jobs():
    pool, _, _ = make_pool()
    pool.register(
        RegisterRequest(
            worker_id="w-plain",
            capabilities=frozenset({"repo-code"}),
            capacity=1,
            auth_token="",
            metadata={"lease_compatible": False},
        )
    )
    pool.register(
        RegisterRequest(
            worker_id="w-lease",
            capabilities=frozenset({"repo-code"}),
            capacity=1,
            auth_token="",
            metadata={"lease_compatible": True},
        )
    )
    dispatcher = RemoteDispatcher(pool)
    assignment = dispatcher.submit(
        "repo goal", "repo-code", repo_key="/repos/a", job_id="j1"
    )
    assert assignment is not None and assignment.worker_id == "w-lease"
    free = dispatcher.submit("free goal", "repo-code", job_id="j2")
    assert free is not None


def test_local_adapter_parity_with_direct_execution():
    class ScriptWorker:
        name = "script-bud"
        capabilities = {"code"}

        def health(self):
            return {"name": self.name, "status": "READY"}

        def execute(self, goal, metadata=None):
            return {
                "success": True,
                "output": {"goal": goal, "echo": metadata.get("k")},
                "evidence": [{"type": "local", "goal": goal}],
                "retryable": False,
            }

    pool, _, _ = make_pool()
    direct = ScriptWorker().execute("hello", {"k": "v"})
    adapter = LocalWorkerAdapter(ScriptWorker(), pool, worker_id="local-1")
    dispatcher = RemoteDispatcher(pool)
    assignment = dispatcher.submit(
        "hello", "code", metadata={"k": "v"}, job_id="j1"
    )
    assert assignment is not None and assignment.worker_id == "local-1"
    stored = pool.result_for("j1")
    assert stored is not None and stored.success == direct["success"]
    assert stored.output == direct["output"]
    assert stored.evidence == direct["evidence"]
    assert adapter.heartbeat() is True


def test_worker_health_state_and_drain_mode():
    pool, _, clock = make_pool()
    pool.register(
        RegisterRequest(
            worker_id="w-1",
            capabilities=frozenset({"code"}),
            capacity=1,
            auth_token="",
        )
    )
    assert pool.set_healthy("w-1", False) is True
    assert pool.get("w-1").state.value == "UNHEALTHY"
    assert pool.dispatch(JobSpec(job_id="j1", goal="g", capability="code")) is None
    assert pool.set_healthy("w-1", True) is True
    assert pool.set_drain("w-1", True) is True
    assert pool.get("w-1").state.value == "DRAINING"
    assert pool.dispatch(JobSpec(job_id="j2", goal="g", capability="code")) is None
    pool.heartbeat(HeartbeatMessage(worker_id="w-1", healthy=True))
    assert pool.get("w-1").state.value == "DRAINING"
    assert pool.set_drain("w-1", False) is True
    assert pool.dispatch(JobSpec(job_id="j3", goal="g", capability="code")) is not None


def test_remote_registry_remote_status_surface():
    registry = RemoteWorkerRegistry()
    adapter = registry.attach_local(worker_id="local-1")
    assert adapter.worker_id == "local-1"
    assignment = registry.dispatch_remote("hello", "code", job_id="j1")
    assert assignment is not None
    assert registry.pool.result_for("j1") is not None
    statuses = registry.remote_status()
    assert statuses[0]["worker_id"] == "local-1"
    assert statuses[0]["state"] == "READY"
