"""Worker R: cancel propagation, idempotent results, protocol/auth.

Hermetic: in-memory transport, injected clock, no network.
"""

from __future__ import annotations

import pytest

from app.remote import (
    AuthRejectedError,
    CancelRequest,
    FakeRemoteWorker,
    InMemoryTransport,
    JobResult,
    JobSpec,
    ProtocolMismatchError,
    RegisterRequest,
    RemoteWorkerPool,
    StaticTokenAuthenticator,
)


class Clock:
    def __init__(self):
        self.t = 2000.0

    def __call__(self):
        return self.t


def make_pool(**kwargs):
    clock = Clock()
    transport = InMemoryTransport()
    pool = RemoteWorkerPool(transport=transport, clock=clock, **kwargs)
    return pool, transport, clock


def req(worker_id="w-1", protocol=1, token="", capabilities=("code",)):
    return RegisterRequest(
        worker_id=worker_id,
        capabilities=frozenset(capabilities),
        capacity=1,
        protocol_version=protocol,
        auth_token=token,
    )


def jid(job_id, capability="code"):
    return JobSpec(job_id=job_id, goal="g", capability=capability)


def ok(job_id, worker_id, evidence=None):
    return JobResult(
        job_id=job_id,
        worker_id=worker_id,
        success=True,
        output={"goal": "g"},
        evidence=evidence or [{"type": "step", "job_id": job_id}],
    )


def test_cancel_propagation_reaches_worker():
    pool, transport, _ = make_pool()
    pool.register(req("w-1"))
    worker = FakeRemoteWorker("w-1", transport)
    worker.on_result = pool.submit_result
    assignment = pool.dispatch(jid("j1"))
    assert assignment is not None
    assert pool.result_for("j1") is not None
    pool2, transport2, _ = make_pool()
    pool2.register(req("w-9"))
    deferred: list = []
    transport2.attach_worker("w-9", deferred.append)
    assert pool2.dispatch(jid("j9")) is not None
    assert pool2.cancel_job("j9", reason="user stop") is True
    assert transport2.sent_cancels == [CancelRequest(job_id="j9", reason="user stop")]
    assert worker.cancels == []


def test_duplicate_result_submission_is_idempotent():
    pool, _, _ = make_pool()
    pool.register(req("w-1"))
    pool.dispatch(jid("j1"))
    first = pool.submit_result(ok("j1", "w-1"))
    second = pool.submit_result(ok("j1", "w-1"))
    assert first == "accepted"
    assert second == "duplicate"
    assert pool.submit_result(ok("ghost", "w-1")) == "unknown"
    stored = pool.result_for("j1")
    assert stored is not None and stored.success is True
    assert pool.get("w-1").used_slots == 0


def test_protocol_mismatch_rejected():
    pool, _, _ = make_pool()
    with pytest.raises(ProtocolMismatchError):
        pool.register(req("w-old", protocol=0))
    with pytest.raises(ProtocolMismatchError):
        pool.register(req("w-new", protocol=999))


def test_auth_rejection_with_static_token():
    pool, _, _ = make_pool(authenticator=StaticTokenAuthenticator("s3cret"))
    with pytest.raises(AuthRejectedError):
        pool.register(req("w-1", token="wrong"))
    response = pool.register(req("w-1", token="s3cret"))
    assert response.accepted


def test_evidence_roundtrip_preserved():
    pool, _, _ = make_pool()
    pool.register(req("w-1"))
    pool.dispatch(jid("j1"))
    evidence = [
        {"type": "diff", "file": "a.py", "lines": 3},
        {"type": "test", "name": "test_a", "passed": True},
    ]
    assert pool.submit_result(ok("j1", "w-1", evidence=evidence)) == "accepted"
    stored = pool.result_for("j1")
    assert stored is not None and stored.evidence == evidence
