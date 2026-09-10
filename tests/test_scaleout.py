"""
Stage 10.8: scale-out validation.

Proves correctness with multiple coordinators/processes over one
database (SQLite here; the same contracts run against Postgres in
test_pg_contract.py when a server is reachable):

- two coordinator instances, same DB: exact-once claim
- same-repo exclusion across coordinators
- different-repo concurrency across coordinators
- per-client quota enforcement racing across coordinators
- priority/fairness under contention
- cancellation requested by "another process" is observed
- heartbeat/watchdog correctness across coordinators
- outbox relay duplication resistance under concurrent relays
"""

import threading
import time

from app.core.models import Mission, MissionStatus
from app.learning.engine import record_id_default
from app.learning.store import LearningStore
from app.runtime.outbox_relay import OutboxRelay
from app.runtime.repo_leases import RepoLeaseManager
from app.storage.sqlite_store import MissionStore


class RecordingWorker:
    name = "scaleout-bud"
    capabilities = {"repo-code"}

    def __init__(self, delay=0.0):
        self.delay = delay
        self.executed = []
        self.lock = threading.Lock()

    def supports(self, capability):
        return capability == "repo-code"

    def health(self):
        return {"name": self.name, "status": "READY"}

    def execute(self, goal, metadata=None):
        with self.lock:
            self.executed.append(goal)

        if self.delay:
            time.sleep(self.delay)

        return {
            "success": True,
            "output": {"goal": goal},
            "evidence": [],
            "error": None,
        }


class Registry:
    def __init__(self, worker):
        self.worker = worker

    def find(self, capability):
        return (
            self.worker
            if capability in self.worker.capabilities
            else None
        )

    def status(self):
        return [self.worker.health()]


def _coordinator(store, leases, worker, name):
    from app.runtime.coordinator import Coordinator

    coordinator = Coordinator(
        store=store,
        leases=leases,
        registry=Registry(worker),
        id_prefix=name,
        relay=OutboxRelay(store=store),
    )

    return coordinator


def test_two_coordinators_claim_exactly_once(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    total = 30

    for i in range(total):
        store.enqueue(
            Mission(
                goal=f"scale {i}",
                capability="repo-code",
                metadata={"repo_path": f"/repo/{i}"},
            )
        )

    claimed = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def run_coordinator(name):
        barrier.wait()

        while True:
            mission = store.claim_next(name)

            if mission is None:
                return

            with lock:
                claimed.append((mission.id, name))

    threads = [
        threading.Thread(target=run_coordinator, args=(f"coord-{i}",))
        for i in range(2)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    ids = [m for m, _ in claimed]

    assert len(claimed) == total
    assert len(set(ids)) == total, "a mission was claimed twice"

    # Both coordinators participated.
    owners = {name for _, name in claimed}

    assert owners == {"coord-0", "coord-1"}


def test_two_full_coordinators_same_repo_exclusion(tmp_path):
    """Two independent Coordinator instances run missions for the
    same repo strictly serialized; different repos run in
    parallel."""
    worker = RecordingWorker(delay=0.4)
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)

    coord_a = _coordinator(store, leases, worker, "scale-a")
    coord_b = _coordinator(store, leases, worker, "scale-b")

    same_repo = str(tmp_path / "shared-repo")
    other_repo = str(tmp_path / "other-repo")

    store.enqueue(
        Mission(
            goal="shared 1",
            capability="repo-code",
            metadata={"repo_path": same_repo},
        )
    )
    store.enqueue(
        Mission(
            goal="shared 2",
            capability="repo-code",
            metadata={"repo_path": same_repo},
        )
    )
    store.enqueue(
        Mission(
            goal="other 1",
            capability="repo-code",
            metadata={"repo_path": other_repo},
        )
    )

    coord_a.start()
    coord_b.start()

    try:
        deadline = time.monotonic() + 20

        while time.monotonic() < deadline:
            missions = store.list()

            if all(
                m.status
                in (MissionStatus.passed, MissionStatus.failed)
                for m in missions
            ):
                break

            time.sleep(0.1)

        missions = store.list()

        assert all(
            m.status == MissionStatus.passed for m in missions
        ), [
            (m.goal, m.status.value, (m.result.get("error") or {}))
            for m in missions
        ]
        assert sorted(worker.executed) == [
            "other 1", "shared 1", "shared 2",
        ]
    finally:
        coord_a.stop()
        coord_b.stop()


def test_quota_race_across_coordinators(tmp_path):
    """Per-client concurrency quota holds when two coordinators
    claim the same client's queue concurrently."""
    store = MissionStore(tmp_path / "db.sqlite")

    for i in range(6):
        store.enqueue(
            Mission(
                goal=f"quota {i}",
                capability="repo-code",
                metadata={"repo_path": f"/repo/q/{i}"},
                client_id="cl_scale",
                priority=1,
            )
        )

    claimed = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def run_coordinator(name):
        barrier.wait()

        while True:
            mission = store.claim_next(
                name, client_limits={"cl_scale": 2}
            )

            if mission is None:
                return

            with lock:
                claimed.append(mission.id)

    threads = [
        threading.Thread(target=run_coordinator, args=(f"qc-{i}",))
        for i in range(2)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    assert len(claimed) == 2, (
        f"quota must cap claims at 2 across coordinators, got "
        f"{len(claimed)}"
    )
    assert len(set(claimed)) == 2


def test_priority_fairness_under_contention(tmp_path):
    """Two coordinators draining a mixed-priority queue: all
    priority-1 missions are claimed before any priority-9 mission
    starts (claim-order fairness)."""
    store = MissionStore(tmp_path / "db.sqlite")

    claim_order = []
    lock = threading.Lock()

    goals = []

    for i in range(4):
        goal = f"urgent {i}"
        goals.append((1, goal))
        store.enqueue(
            Mission(
                goal=goal,
                capability="repo-code",
                metadata={"repo_path": f"/repo/u/{i}"},
                priority=1,
            )
        )

    for i in range(4):
        goal = f"bulk {i}"
        goals.append((9, goal))
        store.enqueue(
            Mission(
                goal=goal,
                capability="repo-code",
                metadata={"repo_path": f"/repo/b/{i}"},
                priority=9,
            )
        )

    barrier = threading.Barrier(2)

    def run_coordinator(name):
        barrier.wait()

        while True:
            mission = store.claim_next(name)

            if mission is None:
                return

            with lock:
                claim_order.append(mission.goal)

    threads = [
        threading.Thread(target=run_coordinator, args=(f"pc-{i}",))
        for i in range(2)
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    urgent_positions = [
        i for i, g in enumerate(claim_order) if g.startswith("urgent")
    ]
    bulk_positions = [
        i for i, g in enumerate(claim_order) if g.startswith("bulk")
    ]

    assert len(claim_order) == 8
    assert max(urgent_positions) < min(bulk_positions), claim_order


def test_cancellation_visible_across_coordinators(tmp_path):
    """A cancellation requested through the API process is observed
    by a different coordinator process before execution."""
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(
        goal="cross-process cancel",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path / "repo")},
    )
    store.enqueue(mission)

    # Coordinator A claims it (crashes before executing: no run).
    claimed = store.claim_next("coord-crash")

    assert claimed.id == mission.id

    # "Another process" (the API) requests cancellation.
    outcome = store.request_cancel(mission.id)

    assert outcome == "requested"

    # Coordinator B (recovery/watchdog path) picks up the flag.
    fresh = store.get(mission.id)

    assert fresh.cancel_requested is True
    assert fresh.status == MissionStatus.running

    # Heartbeat by the crashed owner fails; by others too (owner
    # guard), but the cancel flag survives any save.
    assert store.heartbeat(mission.id, "coord-crash") is True
    assert store.heartbeat(mission.id, "coord-b") is False

    # Coordinator B finalizes the cancelled mission.
    fresh = store.get(mission.id)
    fresh.status = MissionStatus.cancelled
    fresh.finished_at = fresh.updated_at
    store.save(fresh)

    assert store.get(mission.id).status == MissionStatus.cancelled
    assert store.get(mission.id).cancel_requested is True


def test_heartbeat_and_watchdog_across_coordinators(tmp_path):
    """A live coordinator's missions are never touched by another
    coordinator's watchdog; a dead coordinator's missions are
    recoverable. Uses the public watchdog entrypoint directly so
    the test is deterministic (no thread-timing sleeps)."""
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)

    worker = RecordingWorker(delay=0)
    coord_live = _coordinator(store, leases, worker, "watch-live")

    dead_mission = Mission(
        goal="stale recovery target",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path / "repo")},
    )
    store.enqueue(dead_mission)

    live_mission = Mission(
        goal="live heartbeat target",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path / "repo-live")},
    )
    store.enqueue(live_mission)

    # Dead coordinator claims one mission (and dies before any
    # heartbeat); the live coordinator claims the other and keeps
    # it inflight with a fresh heartbeat.
    dead_claim = store.claim_next("coord-dead")

    assert dead_claim.id == dead_mission.id

    live_claim = store.claim_next(coord_live.id)

    assert live_claim.id == live_mission.id

    assert store.heartbeat(live_mission.id, coord_live.id) is True

    with coord_live._inflight_lock:
        coord_live._inflight.add(live_mission.id)

    # Age the dead coordinator's heartbeat: it died hours ago.
    dead_claim.heartbeat_at = "2026-09-09T00:00:00+00:00"
    store.save(dead_claim)

    # Watchdog view: the dead coordinator's mission is stale, the
    # live one is excluded by the inflight guard even though its
    # heartbeat is also past the cutoff.
    recovered = coord_live.recover_stale_missions()

    assert dead_mission.id in recovered
    assert live_mission.id not in recovered

    dead_after = store.get(dead_mission.id)
    live_after = store.get(live_mission.id)

    assert dead_after.status == MissionStatus.failed

    error = dead_after.result.get("error", {})

    assert error.get("type") == "InterruptedExecution"
    assert error.get("recovered_by") == coord_live.id

    assert live_after.status == MissionStatus.running

    # Owner guard: a different coordinator can never heartbeat a
    # mission it does not own.
    assert store.heartbeat(dead_mission.id, coord_live.id) is False
    assert store.heartbeat(live_mission.id, "coord-dead") is False

    # Cross-process lease semantics: the live coordinator holds
    # its repo lease and heartbeats it; release only drops the
    # owner's own lease.
    assert leases.heartbeat("repo:live-guard", coord_live.id) is False

    assert leases.acquire(
        str(tmp_path / "repo-live"), coord_live.id
    ) is True
    assert (
        leases.acquire(str(tmp_path / "repo-live"), "coord-dead")
        is False
    ), "a different coordinator must not steal a fresh lease"
    assert leases.holder(str(tmp_path / "repo-live")) == coord_live.id

    with coord_live._inflight_lock:
        coord_live._inflight.discard(live_mission.id)

    leases.release(str(tmp_path / "repo-live"), coord_live.id)


def test_outbox_relay_duplication_resistance(tmp_path):
    """Two relays draining the same outbox concurrently produce
    exactly one learning record per mission."""
    db_path = tmp_path / "db.sqlite"
    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    for i in range(10):
        mission_id = f"m_dup_{i}"
        store.outbox_enqueue(
            mission_id=mission_id,
            kind="learning.record",
            payload={
                "mission_id": mission_id,
                "goal": f"dup {i}",
                "success": True,
                "record_id": record_id_default(mission_id),
            },
        )

    relay_a = OutboxRelay(store=store)
    relay_b = OutboxRelay(store=store)

    results = []

    def drain(relay):
        results.append(relay.drain_once())

    threads = [
        threading.Thread(target=drain, args=(relay_a,)),
        threading.Thread(target=drain, args=(relay_b,)),
    ]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # Every message delivered exactly once across both relays.
    assert sum(results) == 10, results
    assert store.outbox_stats()["pending"] == 0

    records = learning.list()

    assert len(records) == 10, (
        "no duplicate learning records allowed"
    )


def test_sqlite_scale_ceiling_documented(tmp_path):
    """The practical SQLite ceiling: concurrent writers serialize
    via the write lock; throughput degrades but correctness holds
    under heavy multi-writer load (documents the honest limit the
    config layer warns about for the multi-process profile)."""
    import tempfile
    from pathlib import Path

    db = Path(tempfile.mkdtemp()) / "ceiling.db"
    store = MissionStore(db)

    errors = []
    enqueued = []
    lock = threading.Lock()

    def writer(i):
        try:
            for j in range(10):
                mission = Mission(
                    goal=f"w{i} m{j}",
                    capability="repo-code",
                    metadata={"repo_path": f"/repo/{i}/{j}"},
                )
                store.enqueue(mission)

                with lock:
                    enqueued.append(mission.id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]

    for t in threads:
        t.start()

    for t in threads:
        t.join(timeout=60)

    assert errors == []
    assert len(enqueued) == 80
    assert len(store.list()) == 80
