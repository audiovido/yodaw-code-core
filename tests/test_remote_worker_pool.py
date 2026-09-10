"""Worker R: remote worker pool registration, routing, liveness.

Hermetic: in-memory transport, injected clock, no network.
"""

from __future__ import annotations

import pytest

from app.remote import (
    DuplicateIdentityError,
    HeartbeatMessage,
    InMemoryTransport,
    JobResult,
    JobSpec,
    RegisterRequest,
    RemoteWorkerPool,
)
from app.remote.dispatcher import RemoteDispatcher


class Clock:
    def __init__(self):
        self.t = 1000.0

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


def reg(
    worker_id="w-1",
    capabilities=("code",),
    capacity=2,
    protocol=1,
    token="",
    **meta,
):
    return RegisterRequest(
        worker_id=worker_id,
        capabilities=frozenset(capabilities),
        capacity=capacity,
        protocol_version=protocol,
        auth_token=token,
        metadata=dict(meta),
    )


def spec(job_id="job_000001", capability="code", repo_key=""):
    return JobSpec(job_id=job_id, goal="do work", capability=capability, repo_key=repo_key)


def test_register_assigns_unique_identity_and_advertises_caps():
    pool, _, _ = make_pool()
    response = pool.register(reg("w-1", ("code", "repo-code"), capacity=2))
    assert response.accepted and response.worker_id == "w-1"
    record = pool.get("w-1")
    assert record is not None
    assert record.worker_id == "w-1"
    assert set(record.capabilities) == {"code", "repo-code"}
    assert record.capacity == 2
    assert record.state.value == "READY"


def test_duplicate_identity_rejected_while_live():
    pool, _, _ = make_pool()
    pool.register(reg("w-1"))
    with pytest.raises(DuplicateIdentityError):
        pool.register(reg("w-1"))


def test_capability_routing_picks_matching_worker():
    pool, _, _ = make_pool()
    pool.register(reg("py", ("code",)))
    pool.register(reg("repo", ("repo-code",)))
    assert pool.dispatch(spec("j1", "repo-code")).worker_id == "repo"
    assert pool.dispatch(spec("j2", "code")).worker_id == "py"
    assert pool.dispatch(spec("j3", "nope")) is None


def test_capacity_blocks_over_dispatch_and_drain_blocks_new_work():
    pool, _, _ = make_pool()
    pool.register(reg("w-1", capacity=1))
    assert pool.dispatch(spec("j1")) is not None
    assert pool.dispatch(spec("j2")) is None
    assert pool.set_drain("w-1", True)
    pool.submit_result(
        JobResult(job_id="j1", worker_id="w-1", success=True, output={}, evidence=[])
    )
    assert pool.dispatch(spec("j3")) is None
    assert pool.set_drain("w-1", False)
    assert pool.dispatch(spec("j4")) is not None


def test_heartbeat_keeps_worker_live_and_stale_worker_evicts():
    pool, _, clock = make_pool()
    pool.register(reg("w-1"))
    assert pool.heartbeat(HeartbeatMessage(worker_id="w-1", used_slots=0))
    assert pool.get("w-1").state.value == "READY"
    clock.advance(60.0)
    assert pool.evict_stale() == ["w-1"]
    assert pool.get("w-1").state.value == "OFFLINE"
    assert pool.heartbeat(HeartbeatMessage(worker_id="w-1")) is False


def test_reconnect_after_disconnect_releases_identity():
    pool, _, _ = make_pool()
    pool.register(reg("w-1", ("code",), capacity=1))
    pool.dispatch(spec("j1"))
    orphaned = pool.disconnect("w-1")
    assert [s.job_id for s in orphaned] == ["j1"]
    response = pool.register(reg("w-1", ("code",)))
    assert response.accepted and response.reason == "reconnected"
    assert pool.dispatch(spec("j2")) is not None


def test_job_reassignment_after_stale_eviction():
    pool, transport, clock = make_pool()
    other_seen: list = []
    transport.attach_worker("w-2", other_seen.append)
    pool.register(reg("w-1", capacity=1))
    pool.register(reg("w-2", capacity=1))
    transport.detach_worker("w-2")
    pool.dispatch(spec("j1"))
    clock.advance(60.0)
    assert pool.evict_stale() != []
    dispatcher = RemoteDispatcher(pool)
    reassigned = dispatcher.reassign_orphans()
    assert [a.job_id for a in reassigned] == []
    pool.register(reg("w-3", capacity=1))
    reassigned = dispatcher.reassign_orphans()
    assert [a.job_id for a in reassigned] == ["j1"]
    assert reassigned[0].worker_id == "w-3"
