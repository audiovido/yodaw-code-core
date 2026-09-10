"""Hermetic DAG scheduling tests: order, parallelism, cycles."""

from __future__ import annotations

import pytest

from app.scheduler.models import TaskSpec, TaskState
from app.scheduler.scheduler import CycleError, Scheduler
from app.scheduler.store import SchedulerStore


def make_scheduler(tmp_path, **kwargs):
    store = SchedulerStore(tmp_path / "sched.sqlite")
    kwargs.setdefault("lease_seconds", 60)
    return Scheduler(store=store, **kwargs)


def submit(sched, specs):
    return sched.submit([TaskSpec(**s) for s in specs])


def claim(sched, worker, task_id):
    claimed = sched.schedule_once(worker)
    assert claimed is not None
    assert claimed.task_id == task_id
    return claimed


def test_dag_order(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    a, b, c = submit(
        sched,
        [
            {"task_id": "a", "goal": "a", "capability": "code"},
            {"task_id": "b", "goal": "b", "capability": "code",
             "dependencies": ["a"]},
            {"task_id": "c", "goal": "c", "capability": "code",
             "dependencies": ["b"]},
        ],
    )
    assert sched.get("b").state == TaskState.blocked
    assert sched.get("c").state == TaskState.blocked
    claim(sched, "w", "a")
    sched.complete_task("a", "w")
    assert sched.get("b").state == TaskState.ready
    claim(sched, "w", "b")
    sched.complete_task("b", "w")
    claim(sched, "w", "c")
    sched.complete_task("c", "w", output=None)
    assert sched.topological_order() == ["a", "b", "c"]


def test_parallel_independent_nodes(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w1", concurrency_limit=4, capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": f"t{i}", "goal": f"g{i}", "capability": "code"}
            for i in range(4)
        ],
    )
    got = set()
    for _ in range(4):
        claimed = sched.schedule_once("w1")
        assert claimed is not None
        sched.start_task(claimed.task_id, "w1")
        got.add(claimed.task_id)
    assert got == {"t0", "t1", "t2", "t3"}


def test_cycle_rejection(tmp_path):
    sched = make_scheduler(tmp_path)
    with pytest.raises(CycleError):
        submit(
            sched,
            [
                {"task_id": "a", "goal": "a", "dependencies": ["b"]},
                {"task_id": "b", "goal": "b", "dependencies": ["a"]},
            ],
        )
    with pytest.raises(CycleError):
        submit(sched, [{"task_id": "s", "goal": "s",
                        "dependencies": ["s"]}])


def test_dependency_failure_propagation(tmp_path):
    sched = make_scheduler(tmp_path)
    sched.register_worker("w", capabilities=["code"])
    submit(
        sched,
        [
            {"task_id": "root", "goal": "root", "capability": "code"},
            {"task_id": "child", "goal": "child", "capability": "code",
             "dependencies": ["root"]},
            {"task_id": "grand", "goal": "grand", "capability": "code",
             "dependencies": ["child"]},
        ],
    )
    claim(sched, "w", "root")
    sched.fail_task("root", "w", "ValidationError", retryable=False)
    assert sched.get("child").state == TaskState.failed
    assert sched.get("grand").state == TaskState.failed


def test_restart_resume_partially_completed_dag(tmp_path):
    path = tmp_path / "sched.sqlite"
    first = Scheduler(store=SchedulerStore(path), lease_seconds=60)
    first.register_worker("w", capabilities=["code"])
    submit(
        first,
        [
            {"task_id": "a", "goal": "a", "capability": "code"},
            {"task_id": "b", "goal": "b", "capability": "code",
             "dependencies": ["a"]},
        ],
    )
    claim(first, "w", "a")
    first.complete_task("a", "w")
    assert first.get("b").state == TaskState.ready
    second = Scheduler(store=SchedulerStore(path), lease_seconds=60)
    second.register_worker("w", capabilities=["code"])
    recovered = second.recover()
    assert recovered == []
    claimed = second.schedule_once("w")
    assert claimed is not None
    assert claimed.task_id == "b"
