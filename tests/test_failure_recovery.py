"""
Worker J: failure taxonomy and recovery audit.

Hermetic: fake httpx transports, real SQLite, fake workers.
No network, no model, no API keys.

Covers:
- provider timeout / 429 / 5xx / auth-failure classification
- retry after partial failure (transient-then-success)
- malformed provider responses (never retried)
- worker crash finalize, lease release, inflight cleanup
- outbox enqueue failure must not fail the mission
- cancel vs complete race
- corrupt-row skip in claim and watchdog scans
- watchdog never clobbers terminal missions
- evidence preservation after failure
"""

import threading
import time
import types

import httpx
import pytest

from app.core.models import Mission, MissionStatus
from app.llm import provider as provider_module
from app.llm.provider import (
    LocalLLMProvider,
    LLMError,
    _is_retryable,
    pop_attempt_log,
)
from app.runtime.coordinator import Coordinator
from app.runtime.outbox_relay import OutboxRelay
from app.runtime.repo_leases import RepoLeaseManager
from app.storage.sqlite_store import MissionStore


# ---------------------------------------------------------
# Provider failure taxonomy (fake transports only)
# ---------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_provider_env(monkeypatch):
    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "3")
    monkeypatch.setenv("YODAW_PROVIDER_BACKOFF_SECONDS", "0.01")
    monkeypatch.setenv("YODAW_LLM_MODEL", "fake-model")
    monkeypatch.setenv("YODAW_LLM_STYLE", "ollama")
    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "5")


def make_fake_httpx(handler):
    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError

    def post(url, json=None, headers=None, timeout=None):
        request = httpx.Request("POST", url)
        return handler(request)

    fake.post = post
    return fake


def run_chat(handler, monkeypatch):
    monkeypatch.setattr(
        provider_module, "httpx", make_fake_httpx(handler)
    )
    provider = LocalLLMProvider()
    provider.base_url = "http://fake"
    return provider._ollama("system", "user")


def status_error(code):
    response = httpx.Response(
        code, request=httpx.Request("POST", "http://x")
    )
    return httpx.HTTPStatusError(
        f"{code}", request=response.request, response=response
    )


def test_timeout_subtypes_are_retryable():
    assert _is_retryable(httpx.ReadTimeout("t")) is True
    assert _is_retryable(httpx.ConnectTimeout("t")) is True
    assert _is_retryable(httpx.WriteTimeout("t")) is True
    assert _is_retryable(httpx.PoolTimeout("t")) is True
    assert _is_retryable(httpx.ConnectError("refused")) is True


def test_auth_failures_are_never_retryable():
    assert _is_retryable(status_error(401)) is False
    assert _is_retryable(status_error(403)) is False


def test_408_request_timeout_is_retryable():
    # A server-side request timeout is transient: the call never
    # ran to completion, so retrying is safe.
    assert _is_retryable(status_error(408)) is True


def test_408_then_success_recovers(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(408, json={}, request=request)
        return httpx.Response(
            200, json={"message": {"content": "ok"}}, request=request
        )

    assert run_chat(handler, monkeypatch) == "ok"
    assert calls["n"] == 2


def test_429_then_success_recovers(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, json={}, request=request)
        return httpx.Response(
            200, json={"message": {"content": "ok"}}, request=request
        )

    assert run_chat(handler, monkeypatch) == "ok"
    assert calls["n"] == 3


def test_500_then_success_recovers(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={}, request=request)
        return httpx.Response(
            200, json={"message": {"content": "ok"}}, request=request
        )

    assert run_chat(handler, monkeypatch) == "ok"
    assert calls["n"] == 2


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_fails_fast_single_attempt(monkeypatch, code):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(code, json={}, request=request)

    with pytest.raises(LLMError):
        run_chat(handler, monkeypatch)

    assert calls["n"] == 1
    assert pop_attempt_log() != []


def test_invalid_json_body_is_not_retried(monkeypatch):
    calls = {"n": 0}

    class BadJSONResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("No JSON could be decoded")

    def handler(request):
        calls["n"] += 1
        return BadJSONResponse()

    with pytest.raises(LLMError):
        run_chat(handler, monkeypatch)

    assert calls["n"] == 1


def test_openai_malformed_choices_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"choices": []}, request=request)

    monkeypatch.setattr(
        provider_module, "httpx", make_fake_httpx(handler)
    )
    provider = LocalLLMProvider()
    provider.base_url = "http://fake"
    provider.style = "openai"

    with pytest.raises(LLMError, match="malformed"):
        provider._openai("system", "user")

    assert calls["n"] == 1


# ---------------------------------------------------------
# Worker crash / restart (coordinator finalization)
# ---------------------------------------------------------

class RecordingWorker:
    name = "recovery-bud"
    capabilities = {"repo-code"}

    def __init__(self, fail=False):
        self.fail = fail
        self.executed = []

    def health(self):
        return {"name": self.name, "status": "READY"}

    def execute(self, goal, metadata=None):
        self.executed.append(goal)
        if self.fail:
            raise RuntimeError("simulated worker crash")
        return {
            "success": True,
            "output": {"goal": goal, "tests_passed": True},
            "evidence": [],
            "error": None,
            "retryable": False,
        }


class Registry:
    def __init__(self, worker):
        self.worker = worker

    def find(self, capability):
        return self.worker

    def status(self):
        return []


def make_coordinator(tmp_path, worker, name="rec"):
    db = tmp_path / f"{name}.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)
    coordinator = Coordinator(
        store=store,
        leases=leases,
        registry=Registry(worker),
        id_prefix=name,
        relay=OutboxRelay(store=store),
    )
    return coordinator, store, leases


def test_worker_crash_finalizes_and_releases_lease(tmp_path):
    worker = RecordingWorker(fail=True)
    coordinator, store, leases = make_coordinator(tmp_path, worker)

    repo_key = str(tmp_path / "crash-repo")
    store.enqueue(
        Mission(
            goal="crash me",
            capability="repo-code",
            metadata={"repo_path": repo_key},
        )
    )
    mission = store.claim_next(coordinator.id)

    coordinator._execute(mission, repo_key)

    final = store.get(mission.id)
    assert final.status == MissionStatus.failed
    assert final.result["error"]["type"] == "RuntimeError"

    # A restart must be able to proceed: no lease held, no
    # inflight residue, repo immediately re-acquirable.
    assert leases.held_by(coordinator.id) == set()
    with coordinator._inflight_lock:
        assert coordinator._inflight == set()
    assert leases.acquire(repo_key, "restart-coord") is True

    events = [e["event_type"] for e in store.events(mission.id)]
    assert "mission.completed" in events


def test_failed_parent_blocks_dependent_mission(tmp_path):
    """
    Regression: _block_dependent_missions used connect(self.path)
    and self.record_event — neither exists on Coordinator, so the
    dependent was never blocked and an AttributeError was silently
    swallowed. Coordinator must block dependents through its real
    store path and record the blocking event.
    """
    worker = RecordingWorker(fail=True)
    coordinator, store, _ = make_coordinator(tmp_path, worker)

    repo_key = str(tmp_path / "dep-repo")
    parent = Mission(
        goal="parent fails",
        capability="repo-code",
        metadata={"repo_path": repo_key},
    )
    store.enqueue(parent)
    parent_claim = store.claim_next(coordinator.id)

    dependent = Mission(
        goal="dependent must not run",
        capability="repo-code",
        metadata={"repo_path": repo_key, "dependencies": [parent.id]},
    )
    store.enqueue(dependent)

    coordinator._execute(parent_claim, repo_key)

    final_parent = store.get(parent.id)
    assert final_parent.status == MissionStatus.failed

    final_dep = store.get(dependent.id)
    assert final_dep.status == MissionStatus.blocked_external
    assert final_dep.result["error"]["type"] == "DependencyFailed"
    assert final_dep.result["error"]["failed_parent"] == parent.id
    assert final_dep.finished_at is not None

    # The dependent was never claimed, so its worker must not run.
    assert worker.executed == ["parent fails"]
    assert "dependent must not run" not in worker.executed

    events = [e["event_type"] for e in store.events(dependent.id)]
    assert "mission.blocked" in events


def test_outbox_enqueue_failure_does_not_fail_mission(tmp_path):
    class FlakyOutboxStore(MissionStore):
        def outbox_enqueue(self, **kwargs):
            raise RuntimeError("outbox unavailable")

    db = tmp_path / "flaky.sqlite"
    store = FlakyOutboxStore(db)
    worker = RecordingWorker()
    coordinator = Coordinator(
        store=store,
        leases=RepoLeaseManager(db),
        registry=Registry(worker),
        id_prefix="flaky",
        relay=OutboxRelay(store=store),
    )

    repo_key = str(tmp_path / "flaky-repo")
    store.enqueue(
        Mission(
            goal="learning outage",
            capability="repo-code",
            metadata={"repo_path": repo_key},
        )
    )
    mission = store.claim_next(coordinator.id)

    coordinator._execute(mission, repo_key)

    assert store.get(mission.id).status == MissionStatus.passed


def test_failed_mission_evidence_survives_reopen(tmp_path):
    worker = RecordingWorker(fail=True)
    coordinator, store, _ = make_coordinator(tmp_path, worker)

    db_path = tmp_path / "rec.sqlite"
    repo_key = str(tmp_path / "repo")
    store.enqueue(
        Mission(
            goal="evidence check",
            capability="repo-code",
            metadata={"repo_path": repo_key},
        )
    )
    mission = store.claim_next(coordinator.id)
    coordinator._execute(mission, repo_key)

    reopened = MissionStore(db_path)
    final = reopened.get(mission.id)
    assert final.status == MissionStatus.failed
    assert final.result["error"]["type"] == "RuntimeError"
    events = [e["event_type"] for e in reopened.events(mission.id)]
    assert "mission.queued" in events
    assert "mission.started" in events
    assert "mission.completed" in events


# ---------------------------------------------------------
# Cancel vs complete race
# ---------------------------------------------------------

def test_completed_mission_cancel_returns_terminal(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    mission = Mission(goal="done", capability="repo-code")
    mission.status = MissionStatus.passed
    mission.finished_at = "already"
    store.save(mission)

    assert store.request_cancel(mission.id) == "terminal"
    assert store.get(mission.id).status == MissionStatus.passed


def test_concurrent_cancel_on_queued_is_exact_once(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    mission = Mission(goal="race cancel", capability="repo-code")
    store.enqueue(mission)

    outcomes = []
    barrier = threading.Barrier(5)

    def canceller():
        barrier.wait()
        outcomes.append(store.request_cancel(mission.id))

    threads = [threading.Thread(target=canceller) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["cancelled", "terminal", "terminal",
                                "terminal", "terminal"]
    assert store.get(mission.id).status == MissionStatus.cancelled
    cancelled_events = [
        e for e in store.events(mission.id)
        if e["event_type"] == "mission.cancelled"
    ]
    assert len(cancelled_events) == 1


# ---------------------------------------------------------
# Storage corruption handling in availability paths
# ---------------------------------------------------------

def _corrupt_payload(store, mission_id):
    import sqlite3

    db = sqlite3.connect(str(store.path))
    db.execute(
        "UPDATE missions SET payload='NOT JSON{{{' WHERE id=?",
        (mission_id,),
    )
    db.commit()
    db.close()


def test_claim_skips_corrupt_row_and_claims_good(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    bad = Mission(
        goal="corrupt", capability="c",
        metadata={"repo_path": "/r/bad"}, priority=1,
    )
    store.enqueue(bad)
    good = Mission(
        goal="good", capability="c",
        metadata={"repo_path": "/r/good"}, priority=5,
    )
    store.enqueue(good)
    _corrupt_payload(store, bad.id)

    claimed = store.claim_next("coordA")

    # One corrupt row must not wedge the queue: the healthy
    # mission is still claimed exactly once.
    assert claimed is not None
    assert claimed.id == good.id
    assert store.claim_next("coordB") is None

    # The corrupt row is left untouched for operator inspection,
    # never claimed and never deleted.
    import sqlite3

    db = sqlite3.connect(str(store.path))
    status = db.execute(
        "SELECT status FROM missions WHERE id=?", (bad.id,)
    ).fetchone()[0]
    db.close()
    assert status == "QUEUED"


def test_watchdog_skips_corrupt_row_and_recovers_good(tmp_path):
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)

    bad = Mission(
        goal="corrupt executing", capability="c",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(bad)
    store.claim_next("dead-1")
    _corrupt_payload(store, bad.id)

    good = Mission(
        goal="good stale", capability="c",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(good)
    store.claim_next("dead-2")
    stored = store.get(good.id)
    stored.heartbeat_at = "2020-01-01T00:00:00+00:00"
    store.save(stored)

    coordinator = Coordinator(
        store=store,
        leases=RepoLeaseManager(db),
        registry=Registry(None),
        id_prefix="wd",
        relay=OutboxRelay(store=store),
    )

    recovered = coordinator.recover_stale_missions()

    assert recovered == [good.id]
    assert store.get(good.id).status == MissionStatus.failed


# ---------------------------------------------------------
# Watchdog must never clobber terminal missions
# ---------------------------------------------------------

def test_watchdog_skips_terminal_mission_with_stale_heartbeat(tmp_path):
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)

    mission = Mission(
        goal="finished late", capability="c",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)
    store.claim_next("coord-dead")

    # The mission completed (PASS) but the heartbeat timestamp was
    # never refreshed afterwards: recovery must not mistake it for
    # a crash and overwrite the terminal result.
    done = store.get(mission.id)
    done.status = MissionStatus.passed
    done.result = {"commit_sha": "abc123"}
    done.finished_at = "2026-01-01T00:00:00+00:00"
    done.heartbeat_at = "2020-01-01T00:00:00+00:00"
    store.save(done)

    coordinator = Coordinator(
        store=store,
        leases=RepoLeaseManager(db),
        registry=Registry(None),
        id_prefix="wd",
        relay=OutboxRelay(store=store),
    )

    assert coordinator.recover_stale_missions() == []

    final = store.get(mission.id)
    assert final.status == MissionStatus.passed
    assert final.result == {"commit_sha": "abc123"}
    assert "mission.recovered" not in [
        e["event_type"] for e in store.events(mission.id)
    ]


def test_watchdog_still_recovers_genuinely_stale_running(tmp_path):
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)

    mission = Mission(
        goal="stale running", capability="c",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)
    store.claim_next("coord-dead")
    stored = store.get(mission.id)
    stored.heartbeat_at = "2020-01-01T00:00:00+00:00"
    store.save(stored)

    coordinator = Coordinator(
        store=store,
        leases=RepoLeaseManager(db),
        registry=Registry(None),
        id_prefix="wd",
        relay=OutboxRelay(store=store),
    )

    assert coordinator.recover_stale_missions() == [mission.id]

    final = store.get(mission.id)
    assert final.status == MissionStatus.failed
    assert final.result["error"]["type"] == "InterruptedExecution"


def test_retry_after_partial_failure_keeps_attempt_evidence(tmp_path):
    # A coordinator crash mid-repair followed by watchdog recovery
    # preserves the partial attempt evidence already recorded.
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)

    mission = Mission(
        goal="partial", capability="c",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)
    store.claim_next("dead-coord")
    store.record_event(
        mission.id, "validation.started", attempt=0, data={"try": 1}
    )
    stored = store.get(mission.id)
    stored.evidence.append({"type": "validation_failure", "attempt": 0})
    stored.heartbeat_at = "2020-01-01T00:00:00+00:00"
    store.save(stored)

    coordinator = Coordinator(
        store=store,
        leases=RepoLeaseManager(db),
        registry=Registry(None),
        id_prefix="wd",
        relay=OutboxRelay(store=store),
    )
    assert coordinator.recover_stale_missions() == [mission.id]

    final = store.get(mission.id)
    assert final.status == MissionStatus.failed
    assert {"type": "validation_failure", "attempt": 0} in final.evidence
    assert any(
        e.get("type") == "recovery_inspection" for e in final.evidence
    )
    assert [
        e["event_type"] for e in store.events(mission.id)
    ].count("mission.recovered") == 1


def wait_terminal(store, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        missions = store.list()
        if missions and missions[0].status in (
            MissionStatus.passed, MissionStatus.failed,
            MissionStatus.cancelled,
        ):
            return missions[0]
        time.sleep(0.05)
    raise AssertionError("mission never reached a terminal state")
