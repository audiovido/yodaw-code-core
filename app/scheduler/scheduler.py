"""Production multi-agent scheduler.

Pipeline: GOAL -> PLAN DAG -> SCHEDULER -> ELIGIBILITY -> CAPACITY
-> CLAIM -> EXECUTE -> VERIFY -> RETRY/RECOVER -> DEPENDENCY
RELEASE -> COMPLETE.

Lease semantics mirror the durable supervisor: atomic claims in
immediate transactions, heartbeat-extended leases, and recovery
of expired leases without duplicate ownership.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.scheduler.models import (
    MergeCandidate,
    MergeStatus,
    SchedulingEvent,
    TaskOutput,
    TaskSpec,
    TaskState,
    WorkerSpec,
    now_iso,
)
from app.scheduler.store import SchedulerStore

logger = logging.getLogger("yodaw.scheduler")

LEASE_SECONDS_DEFAULT = 60
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
AGING_THRESHOLD_DEFAULT = 5
GLOBAL_LIMIT_DEFAULT = 64

NON_RETRYABLE_ERRORS = frozenset(
    {"ValidationError", "AuthError", "Cancelled", "DependencyFailed"}
)
EXTERNAL_ERRORS = frozenset(
    {"ProviderOutage", "ProviderError", "UpstreamUnavailable"}
)


class CycleError(ValueError):
    """A plan DAG contains a dependency cycle."""


def backoff_delay_seconds(
    retries: int,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_MAX_SECONDS,
) -> float:
    delay = base * (2.0 ** max(0, retries - 1))
    return min(cap, delay)


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class Scheduler:
    """Durable DAG-aware multi-agent scheduler."""

    def __init__(
        self,
        store: SchedulerStore | None = None,
        scheduler_id: str | None = None,
        lease_seconds: int = LEASE_SECONDS_DEFAULT,
        global_limit: int = GLOBAL_LIMIT_DEFAULT,
        aging_threshold: int = AGING_THRESHOLD_DEFAULT,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
    ):
        import uuid

        self.store = store or SchedulerStore()
        self.scheduler_id = scheduler_id or f"sched_{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.global_limit = global_limit
        self.aging_threshold = max(1, aging_threshold)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._lock = threading.Lock()

    # ------------------------------------------------- plan intake
    def submit(
        self,
        tasks: list[TaskSpec | dict],
    ) -> list[TaskSpec]:
        """Admit one plan DAG; reject cycles deterministically."""
        specs = [
            t if isinstance(t, TaskSpec) else TaskSpec.model_validate(t)
            for t in tasks
        ]
        ids = [t.task_id for t in specs]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task_id in plan")
        known = {t.task_id for t in self.store.list_all()} | set(ids)
        for task in specs:
            for dep in task.dependencies:
                if dep == task.task_id:
                    raise CycleError(f"task {task.task_id} depends on itself")
                if dep not in known:
                    raise ValueError(f"unknown dependency {dep}")
        self._reject_cycles(specs)
        for task in specs:
            task.state = TaskState.queued
        self.store.create_tasks(specs)
        for task in specs:
            self.store.record_event(
                task.task_id, "queued", {"goal": task.goal}
            )
        self.refresh_readiness()
        return specs

    def _reject_cycles(self, specs: list[TaskSpec]) -> None:
        graph: dict[str, list[str]] = {
            t.task_id: list(t.dependencies) for t in specs
        }
        existing = {t.task_id: t for t in self.store.list_all()}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str, stack: list[str]) -> None:
            if node in visited:
                return
            if node in visiting:
                cycle = stack[stack.index(node):] + [node] if node in stack else [node]
                raise CycleError(f"dependency cycle: {' -> '.join(cycle)}")
            visiting.add(node)
            stack.append(node)
            deps = graph.get(
                node,
                existing[node].dependencies if node in existing else [],
            )
            for dep in deps:
                visit(dep, stack)
            stack.pop()
            visiting.discard(node)
            visited.add(node)

        for task in specs:
            visit(task.task_id, [])

    def topological_order(self) -> list[str]:
        """Deterministic dependency order (Kahn, seq tie-break)."""
        tasks = {t.task_id: t for t in self.store.list_all()}
        indegree = {tid: 0 for tid in tasks}
        children: dict[str, list[str]] = {tid: [] for tid in tasks}
        for tid, task in tasks.items():
            for dep in task.dependencies:
                if dep in tasks:
                    indegree[tid] += 1
                    children[dep].append(tid)
        seq = {tid: tasks[tid].created_seq for tid in tasks}
        frontier = sorted(
            [tid for tid, deg in indegree.items() if deg == 0],
            key=lambda tid: (seq[tid], tid),
        )
        order: list[str] = []
        while frontier:
            node = frontier.pop(0)
            order.append(node)
            for child in sorted(children[node], key=lambda c: (seq[c], c)):
                indegree[child] -= 1
                if indegree[child] == 0:
                    frontier.append(child)
            frontier.sort(key=lambda tid: (seq[tid], tid))
        return order

    # ------------------------------------------------- workers
    def register_worker(
        self,
        worker_id: str,
        concurrency_limit: int = 1,
        capabilities: list[str] | None = None,
    ) -> WorkerSpec:
        worker = WorkerSpec(
            worker_id=worker_id,
            concurrency_limit=max(1, concurrency_limit),
            capabilities=list(capabilities or []),
        )
        self.store.upsert_worker(worker)
        return worker

    def heartbeat_worker(self, worker_id: str) -> bool:
        return self.store.heartbeat_worker(worker_id)

    # ------------------------------------------------- readiness
    def refresh_readiness(self) -> list[str]:
        """Release dependencies whose parents all completed.

        State transitions use the store's atomic transaction so
        concurrent dispatch or recovery cannot be lost.
        """
        now_s = now_iso()
        released: list[str] = []
        for task in self.store.list_all():
            if task.state in (
                TaskState.assigned,
                TaskState.running,
                TaskState.completed,
                TaskState.failed,
                TaskState.cancelled,
                TaskState.blocked_external,
            ):
                continue
            if task.state == TaskState.retry_wait:
                if task.next_retry_at and task.next_retry_at > now_s:
                    continue
                updated = self.store.transact_task(task.task_id, lambda t: self._retry_to_recovering(t, now_s))
                if updated is not None:
                    self.store.record_event(task.task_id, "ready", {})
                    released.append(task.task_id)
                continue
            deps = [self.store.get(dep) for dep in task.dependencies]
            if any(d is None for d in deps):
                updated = self.store.transact_task(task.task_id, lambda t: self._to_blocked(t, "missing_dependency"))
                continue
            failed = [d for d in deps if d.state in (TaskState.failed, TaskState.cancelled)]
            if failed:
                updated = self.store.transact_task(task.task_id, lambda t: self._dep_failed(t, failed))
                continue
            if all(d.state == TaskState.completed for d in deps):
                updated = self.store.transact_task(task.task_id, lambda t: self._to_ready(t))
                if updated is not None:
                    self.store.record_event(task.task_id, "ready", {})
                    released.append(task.task_id)
            else:
                updated = self.store.transact_task(task.task_id, lambda t: self._to_blocked(t, "waiting_on_dependencies"))
        return released

    def _retry_to_recovering(self, task: TaskSpec, now_s: str) -> TaskSpec | None:
        if task.state != TaskState.retry_wait:
            return None
        if task.next_retry_at and task.next_retry_at > now_s:
            return None
        task.state = TaskState.recovering
        task.next_retry_at = None
        return task

    def _to_blocked(self, task: TaskSpec, reason: str) -> TaskSpec | None:
        if task.state in (TaskState.assigned, TaskState.running, TaskState.completed, TaskState.failed, TaskState.cancelled, TaskState.blocked_external):
            return None
        if task.state == TaskState.blocked:
            return None
        task.state = TaskState.blocked
        task.lease_owner = None
        task.lease_expiry = None
        return task

    def _dep_failed(self, task: TaskSpec, failed: list[TaskSpec]) -> TaskSpec | None:
        if task.state in (TaskState.assigned, TaskState.running, TaskState.completed, TaskState.failed, TaskState.cancelled, TaskState.blocked_external):
            return None
        task.state = TaskState.failed
        task.last_error_type = "DependencyFailed"
        task.last_error_message = ",".join(d.task_id for d in failed)
        task.lease_owner = None
        task.lease_expiry = None
        return task

    def _to_ready(self, task: TaskSpec) -> TaskSpec | None:
        if task.state in (TaskState.assigned, TaskState.running, TaskState.completed, TaskState.failed, TaskState.cancelled, TaskState.blocked_external):
            return None
        if task.state in (TaskState.ready, TaskState.recovering):
            return None
        task.state = TaskState.ready
        return task

    def effective_priority(self, task: TaskSpec) -> int:
        boost = task.wait_rounds // self.aging_threshold
        return max(1, task.priority - boost)

    def ready_queue(self, worker_id: str | None = None) -> list[TaskSpec]:
        """Eligible tasks in dispatch order (no capacity check)."""
        worker = self.store.get_worker(worker_id) if worker_id else None
        out = []
        for task in self.store.list_all():
            if task.state not in (TaskState.ready, TaskState.recovering):
                continue
            if worker is not None and task.capability not in worker.capabilities:
                continue
            out.append(task)
        out.sort(
            key=lambda t: (self.effective_priority(t), t.created_seq, t.task_id)
        )
        return out

    # ------------------------------------------------- dispatch
    def _eligible(self, task: TaskSpec, worker: WorkerSpec) -> bool:
        if task.state not in (TaskState.ready, TaskState.recovering):
            return False
        if task.capability not in worker.capabilities:
            return False
        if task.next_retry_at and task.next_retry_at > now_iso():
            return False
        return True

    def schedule_once(self, worker_id: str) -> TaskSpec | None:
        """Assign at most one task; None when nothing is claimable."""
        worker = self.store.get_worker(worker_id)
        if worker is None:
            raise ValueError(f"unknown worker {worker_id}")
        with self._lock:
            self.refresh_readiness()
            if self.store.load(worker_id) >= worker.concurrency_limit:
                return None
            if self.store.global_load() >= self.global_limit:
                self._age(self.ready_queue(worker_id))
                return None
            candidates = [
                t for t in self.ready_queue(worker_id)
                if self._eligible(t, worker)
            ]
            for task in candidates:
                claimed = self.store.claim_task(
                    task.task_id, worker_id, self.lease_seconds
                )
                if claimed is not None:
                    self.store.record_event(
                        task.task_id,
                        "assigned",
                        {"worker_id": worker_id},
                    )
                    rest = [t for t in candidates if t.task_id != task.task_id]
                    self._age(rest)
                    return claimed
            self._age(candidates)
            return None

    def _age(self, tasks: list[TaskSpec]) -> None:
        self.store.age_tasks([t.task_id for t in tasks])

    # ------------------------------------------------- execution
    def _require_lease(self, task_id: str, worker_id: str) -> TaskSpec:
        task = self.store.get(task_id)
        if task is None:
            raise ValueError(f"unknown task {task_id}")
        if task.state in (
            TaskState.completed,
            TaskState.failed,
            TaskState.cancelled,
        ):
            raise ValueError(f"task {task_id} is terminal")
        if task.lease_owner != worker_id:
            if task.lease_owner is None and task.state in (
                TaskState.ready,
                TaskState.recovering,
            ):
                claimed = self.store.claim_task(
                    task_id, worker_id, self.lease_seconds
                )
                if claimed is None:
                    raise ValueError(f"task {task_id} not owned by {worker_id}")
                return claimed
            raise ValueError(f"task {task_id} not owned by {worker_id}")
        if task.lease_expiry and task.lease_expiry < now_iso():
            raise ValueError(f"lease for task {task_id} expired")
        return task

    def heartbeat(self, task_id: str, worker_id: str) -> bool:
        return self.store.heartbeat_task(
            task_id, worker_id, self.lease_seconds
        )

    def start_task(self, task_id: str, worker_id: str) -> TaskSpec:
        task = self._require_lease(task_id, worker_id)
        task.state = TaskState.running
        self.store.save(task)
        self.store.record_event(
            task_id, "assigned", {"worker_id": worker_id, "phase": "running"}
        )
        return task

    def complete_task(
        self,
        task_id: str,
        worker_id: str,
        result: dict | None = None,
        output: TaskOutput | None = None,
    ) -> TaskSpec:
        task = self._require_lease(task_id, worker_id)
        task.state = TaskState.completed
        task.result = dict(result or {})
        task.lease_owner = None
        task.lease_expiry = None
        task.next_retry_at = None
        self.store.save(task)
        if output is not None:
            self._register_merge(task, output)
        self.store.record_event(
            task_id, "completed", {"worker_id": worker_id}
        )
        self.refresh_readiness()
        return task

    def fail_task(
        self,
        task_id: str,
        worker_id: str,
        error_type: str,
        message: str = "",
        retryable: bool = True,
    ) -> TaskSpec:
        task = self._require_lease(task_id, worker_id)
        if error_type in EXTERNAL_ERRORS:
            return self.block_external(task_id, worker_id, message or error_type)
        if (
            not retryable
            or error_type in NON_RETRYABLE_ERRORS
            or task.retries >= task.max_retries
        ):
            task.state = TaskState.failed
            task.last_error_type = error_type
            task.last_error_message = message
            task.lease_owner = None
            task.lease_expiry = None
            self.store.save(task)
            self.store.record_event(
                task_id, "blocked", {"reason": "failed", "error": error_type}
            )
            self.refresh_readiness()
            return task
        task.retries += 1
        delay = backoff_delay_seconds(
            task.retries, self.backoff_base, self.backoff_max
        )
        task.next_retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat()
        task.last_error_type = error_type
        task.last_error_message = message
        task.state = TaskState.retry_wait
        task.lease_owner = None
        task.lease_expiry = None
        self.store.save(task)
        self.store.record_event(
            task.task_id,
            "retry-scheduled",
            {"attempt": task.retries, "delay": delay, "error": error_type},
        )
        self.refresh_readiness()
        return task

    def block_external(
        self, task_id: str, worker_id: str, reason: str = ""
    ) -> TaskSpec:
        task = self._require_lease(task_id, worker_id)
        task.state = TaskState.blocked_external
        task.last_error_type = "BlockedExternal"
        task.last_error_message = reason
        task.lease_owner = None
        task.lease_expiry = None
        self.store.save(task)
        self.store.record_event(task_id, "blocked", {"reason": reason})
        return task

    def unblock_external(self, task_id: str) -> TaskSpec | None:
        task = self.store.get(task_id)
        if task is None or task.state != TaskState.blocked_external:
            return task
        task.state = TaskState.recovering
        task.next_retry_at = None
        self.store.save(task)
        self.store.record_event(task_id, "ready", {"after": "unblocked"})
        self.refresh_readiness()
        return task

    # ------------------------------------------------- preemption
    def preempt(
        self, task_id: str, checkpoint: dict | None = None
    ) -> TaskSpec | None:
        """Safely preempt a preemptible task; None when denied."""
        task = self.store.get(task_id)
        if task is None:
            return None
        if task.state in (
            TaskState.completed,
            TaskState.failed,
            TaskState.cancelled,
        ):
            return None
        if not task.preemptible:
            return None
        if task.state not in (TaskState.assigned, TaskState.running):
            return None
        if checkpoint is not None:
            task.checkpoint = dict(checkpoint)
        elif task.result:
            task.checkpoint = dict(task.result)
        task.state = TaskState.ready
        task.lease_owner = None
        task.lease_expiry = None
        self.store.save(task)
        self.store.record_event(task_id, "preempted", {})
        return task

    def cancel(self, task_id: str) -> TaskSpec | None:
        task = self.store.get(task_id)
        if task is None:
            return None
        if task.state in (
            TaskState.completed,
            TaskState.failed,
            TaskState.cancelled,
        ):
            return task
        task.state = TaskState.cancelled
        task.lease_owner = None
        task.lease_expiry = None
        self.store.save(task)
        self.refresh_readiness()
        return task

    # ------------------------------------------------- recovery
    def recover(
        self, stale_worker_seconds: int | None = None
    ) -> list[str]:
        """Recover expired leases and stale workers after restart.

        Uses per-task atomic transactions so a concurrent claim that
        just took ownership is never overwritten by a stale scan.
        """
        recovered: list[str] = []
        now_s = now_iso()
        stale_cutoff: str | None = None
        if stale_worker_seconds is not None:
            stale_cutoff = (
                datetime.now(timezone.utc)
                - timedelta(seconds=stale_worker_seconds)
            ).isoformat()
        for task in self.store.list_all():
            if task.state not in (TaskState.assigned, TaskState.running):
                continue
            # Use the store's atomic claim check to avoid races.
            # If another worker holds a live lease, we skip.
            if task.lease_expiry and task.lease_expiry >= now_s:
                if stale_cutoff is None or not task.lease_owner:
                    continue
                worker = self.store.get_worker(task.lease_owner)
                if worker is not None and worker.heartbeat_at >= stale_cutoff:
                    continue
            # Attempt atomic recovery via store transaction.
            updated = self.store.transact_task(task.task_id, lambda t: self._recover_task(t, now_s))
            if updated is not None:
                self.store.record_event(task.task_id, "recovered", {})
                recovered.append(task.task_id)
        if recovered:
            self.refresh_readiness()
        return recovered

    def _recover_task(self, task: TaskSpec, now_s: str) -> TaskSpec | None:
        """Return updated task for recovery, or None to skip."""
        if task.state not in (TaskState.assigned, TaskState.running):
            return None
        if task.lease_expiry and task.lease_expiry >= now_s:
            return None
        task.state = TaskState.recovering
        task.lease_owner = None
        task.lease_expiry = None
        return task

    # ------------------------------------------------- merge coordination
    def _register_merge(self, task: TaskSpec, output: TaskOutput) -> MergeCandidate:
        candidate = MergeCandidate(
            task_id=task.task_id,
            patch_id=output.patch_id,
            touched_files=list(output.touched_files),
            worktree_ref=output.worktree_ref,
        )
        touched = set(candidate.touched_files)
        conflicts: list[MergeCandidate] = []
        for other in self.store.active_candidates():
            if other.task_id == candidate.task_id:
                continue
            if candidate.patch_id and candidate.patch_id == other.patch_id:
                conflicts.append(other)
            elif touched and touched & set(other.touched_files):
                conflicts.append(other)
        if conflicts:
            candidate.status = MergeStatus.conflict
            for other in conflicts:
                if other.status in (MergeStatus.pending, MergeStatus.ready):
                    other.status = MergeStatus.conflict
                    self.store.save_candidate(other)
        else:
            candidate.status = MergeStatus.ready
        self.store.create_candidate(candidate)
        return candidate

    def merge_candidate(self, candidate_id: str) -> MergeCandidate | None:
        return self.store.get_candidate(candidate_id)

    def candidates_for_task(self, task_id: str) -> list[MergeCandidate]:
        return [
            c for c in self.store.all_candidates() if c.task_id == task_id
        ]

    def conflicting_candidates(self) -> list[MergeCandidate]:
        return [
            c
            for c in self.store.all_candidates()
            if c.status == MergeStatus.conflict
        ]

    def request_review(self, candidate_id: str) -> MergeCandidate | None:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            return None
        candidate.status = MergeStatus.needs_review
        self.store.save_candidate(candidate)
        return candidate

    def resolve_candidate(
        self, candidate_id: str, status: MergeStatus
    ) -> MergeCandidate | None:
        candidate = self.store.get_candidate(candidate_id)
        if candidate is None:
            return None
        if status not in (MergeStatus.merged, MergeStatus.resolved):
            raise ValueError("resolution must be merged or resolved")
        candidate.status = status
        self.store.save_candidate(candidate)
        return candidate

    # ------------------------------------------------- inspection
    def get(self, task_id: str) -> TaskSpec | None:
        return self.store.get(task_id)

    def events(self, task_id: str) -> list[SchedulingEvent]:
        return self.store.events(task_id)

    def event_types(self, task_id: str) -> list[str]:
        return [e.event_type for e in self.store.events(task_id)]

    def stats(self) -> dict:
        counts: dict[str, int] = {}
        for task in self.store.list_all():
            counts[task.state.value] = counts.get(task.state.value, 0) + 1
        return {
            "scheduler": self.scheduler_id,
            "tasks": counts,
            "global_load": self.store.global_load(),
        }
