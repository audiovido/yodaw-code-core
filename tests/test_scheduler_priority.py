"""Hermetic priority, FIFO, aging, and capacity tests."""

from __future__ import annotations

from app.scheduler.models import TaskSpec
from app.scheduler.scheduler import Scheduler
from app.scheduler.store import SchedulerStore


def make_scheduler(tmp_path, **kwargs):
    store = SchedulerStore(tmp_path / "sched.sqlite")
    kwargs.setdefault("lease_seconds", 60)
    return Scheduler(store=store, **kwargs)


def submit(sched, specs):
    return sched.submit([TaskSpec(**s) for s in specs])


def drain(sched, worker, count):
    order = []
    for _ in range(count):
        claimed = sched.schedule_once(worker)
        assert claimed is not None
        order.append(claimed.task_id)
    return order


def test_priority_order(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", concurrency_limit=8, capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "low", "goal": "l", "capability": "code", "priority": 9},
            {"task_id": "high", "goal": "h", "capability": "code", "priority": 1},
            {"task_id": "mid", "goal": "m", "capability": "code", "priority": 5},
        ],
    )
    assert drain(sched, "w", 3) == ["high", "mid", "low"]


def test_fifo_tie_break(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", concurrency_limit=8, capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "first", "goal": "1", "capability": "code"},
            {"task_id": "second", "goal": "2", "capability": "code"},
            {"task_id": "third", "goal": "3", "capability": "code"},
        ],
    )
    assert drain(sched, "w", 3) == ["first", "second", "third"]


def test_starvation_aging(tmp_path):
    sched = make_scheduler(tmp_path, aging_threshold=2)
    sched.register_worker("w", concurrency_limit=8, capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "starved", "goal": "s", "capability": "code",
             "priority": 9},
        ],
    )
    # Let the low-priority task wait several rounds (no dispatch).
    for _ in range(4):
        sched.refresh_readiness()
        queued = sched.ready_queue("w")
        sched._age(queued)
    task = sched.get("starved")
    assert task.wait_rounds >= 4
    assert sched.effective_priority(task) < task.priority


def test_worker_capacity(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", concurrency_limit=1, capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "t1", "goal": "1", "capability": "code"},
            {"task_id": "t2", "goal": "2", "capability": "code"},
        ],
    )
    first = sched.schedule_once("w")
    assert first is not None
    assert sched.schedule_once("w") is None


def test_global_capacity(tmp_path):
    sched = make_scheduler(tmp_path, global_limit=1)
    sched.register_worker("w1", capabilities=["code"])
    sched.register_worker("w2", capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "t1", "goal": "1", "capability": "code"},
            {"task_id": "t2", "goal": "2", "capability": "code"},
        ],
    )
    assert sched.schedule_once("w1") is not None
    assert sched.schedule_once("w2") is None


def test_capability_mismatch(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "gpu-job", "goal": "g", "capability": "gpu"},
            {"task_id": "code-job", "goal": "c", "capability": "code"},
        ],
    )
    claimed = sched.schedule_once("w")
    assert claimed is not None
    assert claimed.task_id == "code-job"
    assert sched.schedule_once("w") is None


def test_deterministic_assignment(tmp_path):
    first = make_scheduler(tmp_path / "a.sqlite")
    second = make_scheduler(tmp_path / "b.sqlite")
    for sched in (first, second):
        sched.register_worker("w", concurrency_limit=8,
                              capabilities=["code"])
        sched.submit(
            [
                TaskSpec(task_id="t1", goal="1", capability="code"),
                TaskSpec(task_id="t2", goal="2", capability="code"),
                TaskSpec(task_id="t3", goal="3", capability="code"),
            ]
        )
    first_order = [t.task_id for t in first.ready_queue("w")]
    second_order = [t.task_id for t in second.ready_queue("w")]
    assert first_order == second_order == ["t1", "t2", "t3"]
