"""
Stage 8.1/8.2/8.3: durable queue, exact-once claim, concurrency
limits, and per-repository exclusion.

All tests are hermetic: real SQLite, real threads, fake workers.
"""

import threading
import time

import pytest

from app.core.models import Mission, MissionStatus
from app.storage.sqlite_store import MissionStore, DuplicateMission
from app.runtime.repo_leases import RepoLeaseManager
from app.runtime.coordinator import Coordinator


class RecordingWorker:
    """Fake worker executing against isolated per-mission repos."""

    name = "recording-bud"
    capabilities = {"repo-code"}

    def __init__(self, delay=0.0, fail=False):
        self.delay = delay
        self.fail = fail
        self.executed = []
        self.lock = threading.Lock()

    def health(self):
        return {"name": self.name, "status": "READY"}

    def execute(self, goal, metadata=None):
        with self.lock:
            self.executed.append(goal)

        if self.delay:
            time.sleep(self.delay)

        if self.fail:
            raise RuntimeError("recording worker failure")

        return {
            "success": True,
            "output": {"goal": goal, "tests_passed": True},
            "evidence": [],
            "error": None,
            "retryable": False,
        }


def make_worker(executor_delay=0.0):
    return RecordingWorker(delay=executor_delay)


def test_enqueue_and_claim_is_exact_once(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(
        goal="do thing",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)

    first = store.claim_next("coordA")
    assert first is not None
    assert first.id == mission.id
    assert first.status == MissionStatus.running
    assert first.claimed_by == "coordA"
    assert first.attempt == 1
    assert first.started_at is not None
    assert first.heartbeat_at is not None

    # Second claimer must not receive the same mission.
    assert store.claim_next("coordB") is None


def test_concurrent_claims_hand_out_each_mission_once(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    total = 25
    for i in range(total):
        store.enqueue(
            Mission(
                goal=f"goal {i}",
                capability="repo-code",
                metadata={"repo_path": f"/repo/{i}"},
            )
        )

    claimed = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def claimer(coordinator_id):
        barrier.wait()

        while True:
            mission = store.claim_next(coordinator_id)

            if mission is None:
                return

            with lock:
                claimed.append((mission.id, coordinator_id))

    threads = [
        threading.Thread(target=claimer, args=(f"c{i}",))
        for i in range(8)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    ids = [m for m, _ in claimed]

    assert len(claimed) == total
    assert len(set(ids)) == total, "a mission was claimed twice"


def test_heartbeat_only_by_owner(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(goal="g", capability="repo-code")
    store.enqueue(mission)
    claimed = store.claim_next("owner1")

    assert store.heartbeat(claimed.id, "owner2") is False
    assert store.heartbeat(claimed.id, "owner1") is True


def test_duplicate_submission_rejected_but_resubmit_ok_after_terminal(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    meta = {"repo_path": "/repo/x"}

    m1 = Mission(goal="same goal", capability="repo-code", metadata=meta)
    store.enqueue(m1)

    with pytest.raises(DuplicateMission):
        store.enqueue(
            Mission(goal="same goal", capability="repo-code", metadata=meta)
        )

    # Different goal, same repo: allowed (different work item).
    m2 = Mission(goal="other goal", capability="repo-code", metadata=meta)
    store.enqueue(m2)

    # Cancel the first; now the same goal may be submitted again.
    assert store.request_cancel(m1.id) == "cancelled"
    store.enqueue(
        Mission(goal="same goal", capability="repo-code", metadata=meta)
    )


class Registry:
    def __init__(self, worker):
        self.worker = worker

    def find(self, capability):
        return self.worker if capability in self.worker.capabilities else None

    def status(self):
        return [self.worker.health()]


def make_coordinator(tmp_path, worker, max_concurrent=4, name="ct"):
    db = tmp_path / f"{name}.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)
    coordinator = Coordinator(
        store=store,
        leases=leases,
        registry=Registry(worker),
        id_prefix=name,
    )
    coordinator.max_concurrent = max_concurrent
    coordinator._pool = __import__(
        "concurrent.futures", fromlist=["ThreadPoolExecutor"]
    ).ThreadPoolExecutor(max_workers=max_concurrent)
    return coordinator, store, leases


def test_coordinator_executes_mission_and_finalizes(tmp_path):
    worker = make_worker()
    coordinator, store, _ = make_coordinator(tmp_path, worker)

    store.enqueue(
        Mission(
            goal="async goal",
            capability="repo-code",
            metadata={"repo_path": str(tmp_path / "repo1")},
        )
    )

    coordinator.start()

    deadline = time.monotonic() + 10

    while time.monotonic() < deadline:
        missions = store.list()

        if missions and missions[0].status in (
            MissionStatus.passed,
            MissionStatus.failed,
        ):
            break

        time.sleep(0.05)

    coordinator.stop()

    mission = store.list()[0]

    assert mission.status == MissionStatus.passed
    assert worker.executed == ["async goal"]

    events = [e["event_type"] for e in store.events(mission.id)]

    assert "mission.queued" in events
    assert "mission.started" in events
    assert "mission.completed" in events


def test_two_repos_run_concurrently_same_repo_serializes(tmp_path):
    worker = RecordingWorker(delay=0.6)
    coordinator, store, _ = make_coordinator(
        tmp_path, worker, max_concurrent=4
    )

    repo_a = str(tmp_path / "repo_a")
    repo_b = str(tmp_path / "repo_b")

    store.enqueue(
        Mission(goal="a1", capability="repo-code", metadata={"repo_path": repo_a})
    )
    store.enqueue(
        Mission(goal="a2", capability="repo-code", metadata={"repo_path": repo_a})
    )
    store.enqueue(
        Mission(goal="b1", capability="repo-code", metadata={"repo_path": repo_b})
    )

    coordinator.start()
    time.sleep(0.25)

    with coordinator._inflight_lock:
        inflight = set(coordinator._inflight)
        repos = set(coordinator._inflight_repos)

    assert len(inflight) == 2, (
        "max concurrency should allow two missions at once"
    )
    assert len(repos) == 2, (
        "two different repos must execute concurrently"
    )

    # Wait for all three to finish.
    deadline = time.monotonic() + 15

    while time.monotonic() < deadline:
        statuses = [m.status for m in store.list()]

        if all(
            s in (MissionStatus.passed, MissionStatus.failed)
            for s in statuses
        ):
            break

        time.sleep(0.05)

    coordinator.stop()

    assert all(
        m.status == MissionStatus.passed for m in store.list()
    ), [m.status for m in store.list()]


def test_max_concurrency_honored_under_load(tmp_path):
    worker = RecordingWorker(delay=0.4)
    coordinator, store, _ = make_coordinator(
        tmp_path, worker, max_concurrent=2
    )

    for i in range(6):
        store.enqueue(
            Mission(
                goal=f"goal {i}",
                capability="repo-code",
                metadata={"repo_path": f"/repo/{i}"},
            )
        )

    peak = {"value": 0}

    import threading as _t

    original_execute = worker.execute

    def counting_execute(goal, metadata=None):
        with coordinator._inflight_lock:
            peak["value"] = max(
                peak["value"], len(coordinator._inflight)
            )
        return original_execute(goal, metadata)

    worker.execute = counting_execute

    coordinator.start()
    time.sleep(3)
    coordinator.stop()

    assert peak["value"] <= 2, peak["value"]


def test_worker_exception_finalizes_mission_as_failed(tmp_path):
    worker = RecordingWorker(fail=True)
    coordinator, store, _ = make_coordinator(tmp_path, worker)

    store.enqueue(
        Mission(
            goal="boom",
            capability="repo-code",
            metadata={"repo_path": str(tmp_path / "r")},
        )
    )

    coordinator.start()

    deadline = time.monotonic() + 10

    while time.monotonic() < deadline:
        missions = store.list()

        if missions and missions[0].status in (
            MissionStatus.passed,
            MissionStatus.failed,
        ):
            break

        time.sleep(0.05)

    coordinator.stop()

    mission = store.list()[0]

    assert mission.status == MissionStatus.failed
    assert "RuntimeError" in mission.result["error"]["type"]

    events = [e["event_type"] for e in store.events(mission.id)]

    assert "mission.completed" in events
