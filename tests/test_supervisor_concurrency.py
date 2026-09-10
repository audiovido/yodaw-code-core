"""Worker N: hermetic multi-worker safety (no network, no I/O)."""

import threading
import time

from app.supervisor.models import JobState
from app.supervisor.repository import SupervisorRepository
from app.supervisor.supervisor import Supervisor


def make_supervisor(repo, worker_id, **kwargs):
    return Supervisor(repo=repo, worker_id=worker_id, **kwargs)


def test_two_workers_claim_same_job_once(tmp_path):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    owner = make_supervisor(repo, "owner")
    job = owner.create_job()
    winners = []
    barrier = threading.Barrier(2)

    def contender(worker_id):
        sup = make_supervisor(repo, worker_id)
        barrier.wait()
        claimed = sup.claim(job.job_id)
        if claimed is not None:
            winners.append(worker_id)

    threads = [
        threading.Thread(target=contender, args=(f"c{i}",))
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


def test_concurrent_claims_hand_out_each_job_once(tmp_path):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    setup = make_supervisor(repo, "setup")
    total = 20
    for _ in range(total):
        setup.create_job()
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def claimer(worker_id):
        sup = make_supervisor(repo, worker_id)
        barrier.wait()
        while True:
            job = sup.claim_next()
            if job is None:
                return
            with lock:
                claimed.append(job.job_id)

    threads = [
        threading.Thread(target=claimer, args=(f"c{i}",)) for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == total
    assert len(set(claimed)) == total


def test_stale_lease_takeover(tmp_path):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    old = make_supervisor(repo, "old", lease_seconds=1)
    new = make_supervisor(repo, "new")
    job = old.create_job()
    assert old.claim(job.job_id) is not None
    time.sleep(1.1)
    taken = new.claim(job.job_id)
    assert taken is not None
    assert taken.lease_owner == "new"


def test_heartbeat_race_keeps_single_owner(tmp_path):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    first = make_supervisor(repo, "w1")
    second = make_supervisor(repo, "w2")
    job = first.create_job()
    first.claim(job.job_id)
    results = []
    barrier = threading.Barrier(2)

    def beat(sup, label):
        barrier.wait()
        for _ in range(10):
            results.append((label, sup.heartbeat(job.job_id)))

    threads = [
        threading.Thread(target=beat, args=(first, "w1")),
        threading.Thread(target=beat, args=(second, "w2")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(ok for label, ok in results if label == "w1")
    assert not any(ok for label, ok in results if label == "w2")
    assert repo.get(job.job_id).lease_owner == "w1"


def test_pause_vs_claim(tmp_path):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    sup = make_supervisor(repo, "w1")
    other = make_supervisor(repo, "w2")
    job = sup.create_job()
    sup.pause(job.job_id)
    assert other.claim(job.job_id) is None
    assert other.claim_next() is None


def test_retry_after_crash(tmp_path):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    crashed = make_supervisor(repo, "crashed", lease_seconds=1)
    job = crashed.create_job()
    crashed.claim(job.job_id)
    time.sleep(1.1)
    rescuer = make_supervisor(repo, "rescuer")
    assert job.job_id in rescuer.recover()
    claimed = rescuer.claim(job.job_id)
    assert claimed is not None
    assert claimed.state == JobState.claimed


def test_duplicate_resume_is_idempotent(tmp_path):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    sup = make_supervisor(repo, "w1")
    job = sup.create_job()
    sup.claim(job.job_id)
    sup.pause(job.job_id)
    first = sup.resume(job.job_id)
    second = sup.resume(job.job_id)
    assert first.state == JobState.recovering
    assert second.state == JobState.recovering
    events = [
        e.event_type for e in repo.events(job.job_id) if e.event_type == "RESUMED"
    ]
    assert len(events) == 2
    assert sup.get(job.job_id).state == JobState.recovering
