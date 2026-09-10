"""Hermetic lease, claim, retry, preemption, and merge tests."""

from __future__ import annotations

import threading

from app.scheduler.models import (
    MergeStatus,
    TaskOutput,
    TaskSpec,
    TaskState,
)
from app.scheduler.scheduler import Scheduler
from app.scheduler.store import SchedulerStore


def make_scheduler(tmp_path, name="sched.sqlite", **kwargs):
    store = SchedulerStore(tmp_path / name)
    kwargs.setdefault("lease_seconds", 60)
    return Scheduler(store=store, **kwargs)


def submit(sched, specs):
    return sched.submit([TaskSpec(**s) for s in specs])


def test_concurrent_scheduler_claims(tmp_path):
    path = tmp_path / "sched.sqlite"
    setup = Scheduler(store=SchedulerStore(path), lease_seconds=60)
    setup.register_worker("w", concurrency_limit=32, capabilities=["code"])
    setup.submit(
        [TaskSpec(task_id=f"t{i}", goal=f"g{i}", capability="code")
         for i in range(20)]
    )
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def claimer(idx):
        sched = Scheduler(store=SchedulerStore(path), lease_seconds=60)
        worker = f"cw{idx}"
        sched.register_worker(worker, concurrency_limit=32,
                              capabilities=["code"])
        barrier.wait()
        while True:
            task = sched.schedule_once(worker)
            if task is None:
                return
            with lock:
                claimed.append(task.task_id)

    threads = [threading.Thread(target=claimer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 20
    assert len(set(claimed)) == 20


def test_stale_lease_recovery(tmp_path):
    path = tmp_path / "sched.sqlite"
    first = Scheduler(store=SchedulerStore(path), lease_seconds=60)
    first.register_worker("w1", capabilities=["code"])
    submit(first, [{"task_id": "t", "goal": "g", "capability": "code"}])
    claimed = first.schedule_once("w1")
    assert claimed is not None
    first.store.force_expire("t")
    second = Scheduler(store=SchedulerStore(path), lease_seconds=60)
    second.register_worker("w2", capabilities=["code"])
    assert second.recover() == ["t"]
    events = second.event_types("t")
    assert "recovered" in events
    # A fresh claim takes ownership without duplication.
    task = second.schedule_once("w2")
    assert task is not None
    assert task.task_id == "t"
    assert task.lease_owner == "w2"


def test_no_duplicate_execution(tmp_path):
    path = tmp_path / "sched.sqlite"
    sched = Scheduler(store=SchedulerStore(path), lease_seconds=60)
    sched.register_worker("w1", capabilities=["code"])
    sched.register_worker("w2", capabilities=["code"])
    submit(sched, [{"task_id": "t", "goal": "g", "capability": "code"}])
    assert sched.schedule_once("w1") is not None
    assert sched.schedule_once("w2") is None


def test_retry_exhaustion(tmp_path):
    sched = make_scheduler(tmp_path, backoff_base=0.0, backoff_max=0.0)
    sched.register_worker("w", capabilities=["code"])
    submit(
        sched,
        [{"task_id": "t", "goal": "g", "capability": "code",
          "max_retries": 2}],
    )
    sched.schedule_once("w")
    sched.fail_task("t", "w", "RuntimeError", retryable=True)
    assert sched.get("t").state == TaskState.recovering
    sched.schedule_once("w")
    sched.fail_task("t", "w", "RuntimeError", retryable=True)
    assert sched.get("t").retries == 2
    sched.schedule_once("w")
    final = sched.fail_task("t", "w", "RuntimeError", retryable=True)
    assert final.state == TaskState.failed
    assert "retry-scheduled" in sched.event_types("t")


def test_blocked_external_distinct(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    submit(sched, [{"task_id": "t", "goal": "g", "capability": "code"}])
    sched.schedule_once("w")
    sched.fail_task("t", "w", "ProviderOutage", retryable=True)
    task = sched.get("t")
    assert task.state == TaskState.blocked_external
    assert "blocked" in sched.event_types("t")
    assert sched.schedule_once("w") is None
    sched.unblock_external("t")
    assert sched.get("t").state in (TaskState.ready, TaskState.recovering)
    assert sched.schedule_once("w") is not None


def test_non_retryable_error(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    submit(sched, [{"task_id": "t", "goal": "g", "capability": "code"}])
    sched.schedule_once("w")
    failed = sched.fail_task("t", "w", "ValidationError", retryable=True)
    assert failed.state == TaskState.failed
    assert failed.retries == 0


def test_safe_preemption(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    submit(
        sched,
        [{"task_id": "t", "goal": "g", "capability": "code",
          "preemptible": True}],
    )
    sched.schedule_once("w")
    sched.start_task("t", "w")
    preempted = sched.preempt("t", checkpoint={"step": 3})
    assert preempted is not None
    assert preempted.state == TaskState.ready
    assert preempted.checkpoint == {"step": 3}
    assert "preempted" in sched.event_types("t")
    # Terminal tasks are never corrupted by preemption.
    preempted.lease_owner = "w"
    sched.complete_task("t", "w")
    assert sched.preempt("t") is None
    assert sched.get("t").state == TaskState.completed


def test_non_preemptible_task(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    submit(
        sched,
        [{"task_id": "t", "goal": "g", "capability": "code",
          "preemptible": False}],
    )
    sched.schedule_once("w")
    sched.start_task("t", "w")
    assert sched.preempt("t") is None
    assert sched.get("t").state == TaskState.running


def test_merge_conflict_state(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", concurrency_limit=8, capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "a", "goal": "a", "capability": "code"},
            {"task_id": "b", "goal": "b", "capability": "code"},
        ],
    )
    sched.schedule_once("w")
    sched.schedule_once("w")
    sched.complete_task(
        "a", "w",
        output=TaskOutput(task_id="a", patch_id="p1",
                          touched_files=["app/x.py"]),
    )
    sched.complete_task(
        "b", "w",
        output=TaskOutput(task_id="b", patch_id="p2",
                          touched_files=["app/x.py"]),
    )
    conflicts = sched.conflicting_candidates()
    assert len(conflicts) == 2
    assert {c.task_id for c in conflicts} == {"a", "b"}
    candidate = sched.candidates_for_task("a")[0]
    reviewed = sched.request_review(candidate.candidate_id)
    assert reviewed.status == MergeStatus.needs_review
    resolved = sched.resolve_candidate(
        candidate.candidate_id, MergeStatus.resolved
    )
    assert resolved.status == MergeStatus.resolved
    # Non-conflicting outputs merge cleanly without review.
    sched.submit([TaskSpec(task_id="c", goal="c", capability="code")])
    sched.schedule_once("w")
    sched.complete_task(
        "c", "w",
        output=TaskOutput(task_id="c", patch_id="p3",
                          touched_files=["app/other.py"]),
    )
    clean = sched.candidates_for_task("c")[0]
    assert clean.status == MergeStatus.ready


def test_scheduling_events_evidence(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    submit(sched, [{"task_id": "t", "goal": "g", "capability": "code"}])
    sched.schedule_once("w")
    sched.complete_task("t", "w")
    types = sched.event_types("t")
    for expected in ("queued", "ready", "assigned", "completed"):
        assert expected in types
