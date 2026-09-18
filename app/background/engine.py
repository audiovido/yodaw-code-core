"""Background job engine: real, durable, browser-independent execution.

A submitted task returns immediately and then runs here, in worker
threads owned by the runtime process — never on the HTTP request path,
never in the browser's connection.

Guarantees this module is responsible for:

- **durable state** — every transition is committed to the task store
  before the corresponding event is emitted
- **isolation** — each task gets a fresh ``git worktree`` on its own
  branch; the user's checked-out branch is never touched, and there is
  no ``reset``, ``clean``, or force-push anywhere in this path
- **cancellation** — a cancel request observed by the store stops the
  running executor's whole process group and ends the task CANCELLED
- **timeout** — one wall-clock budget per task plus per-command bounds
- **PID tracking** — the live executor PID is persisted, so an operator
  (or the crash recovery path) can see what was running
- **crash recovery** — on startup, tasks that were mid-flight are
  requeued with an explicit event, because a fresh worktree is
  allocated per attempt and nothing partial is ever resurrected
- **honest terminal states** — ``COMPLETED`` is reachable only through
  the verifier's PASS *and* a real commit; an executor's prose is never
  evidence
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.background.events import TaskEventBus, get_bus
from app.background.managed_source import resolve_execution_source
from app.background.executors.base import ExecutionContext
from app.background.executors.registry import ExecutorRegistry, default_registry
from app.background.models import (
    PlanResult,
    PlannerError,
    TaskCancelled,
    TaskRecord,
    TaskRequest,
    TaskState,
    TaskTimeout,
    error_class_action,
)
from app.background.planner import GrokArchitect
from app.background.store import TaskStore
from app.background.verifier import TaskVerifier
from app.runtime.repo_identity import (
    RepoNotAllowed,
    canonical_repo_path,
    ensure_authorized,
)
from app.workers import worktree_guard
from app.workers.safe_subprocess import run as run_command
from app.workers.worker_errors import MissionCancelled

logger = logging.getLogger("kodgar.tasks")

DEFAULT_TASK_TIMEOUT = 1800
# Bounded executor fallback: at most this many real execution attempts
# per task, each on a distinct (or transient-retried) executor.
MAX_EXECUTOR_ATTEMPTS = 3
DEFAULT_WORKERS = 2
LOG_FLUSH_SECONDS = 0.5
LOG_FLUSH_LINES = 20
HEARTBEAT_SECONDS = 10


class TaskEngine:
    """Owns the task queue, the worker threads, and the pipeline."""

    def __init__(
        self,
        store: Optional[TaskStore] = None,
        bus: Optional[TaskEventBus] = None,
        registry: Optional[ExecutorRegistry] = None,
        architect: Optional[GrokArchitect] = None,
        verifier_factory: Callable[..., TaskVerifier] = TaskVerifier,
        workers: Optional[int] = None,
        worktree_root: Optional[str | Path] = None,
        timeout_seconds: Optional[int] = None,
    ):
        self.store = store or TaskStore()
        self.bus = bus or get_bus(self.store)
        self.registry = registry or default_registry()
        self.architect = architect or GrokArchitect()
        self.verifier_factory = verifier_factory
        self.worker_count = int(
            workers
            if workers is not None
            else os.environ.get("KODGAR_TASK_WORKERS", str(DEFAULT_WORKERS))
        )
        # Always absolute. A relative root would be interpreted against
        # two different working directories — the *repo's* cwd for
        # `git worktree add` and the *server's* cwd for every later
        # check — silently splitting one task across two directories.
        self.worktree_root = Path(
            worktree_root
            or os.environ.get("KODGAR_TASK_WORKTREE_ROOT", "workspace")
        ).resolve()
        self.timeout_seconds = int(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("KODGAR_TASK_TIMEOUT", str(DEFAULT_TASK_TIMEOUT))
        )
        self.keep_worktrees = _env_flag("KODGAR_TASK_KEEP_WORKTREES", True)
        self._queue: queue.Queue[str] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._running: set[str] = set()
        self._lock = threading.Lock()
        self._started = False
        self.started_at: Optional[str] = None
        self.completed_count = 0
        self.failed_count = 0

    # ----------------------------------------------------- lifecycle
    def start(self) -> list[str]:
        """Start workers and recover interrupted tasks."""
        if self._started:
            return []
        self._started = True
        self.started_at = _now()
        recovered = self.recover()
        for index in range(max(1, self.worker_count)):
            thread = threading.Thread(
                target=self._run_loop,
                name=f"kodgar-task-worker-{index}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        logger.info(
            "task engine started: %s worker(s), %s recovered task(s)",
            self.worker_count,
            len(recovered),
        )
        return recovered

    def stop(self, timeout: float = 20.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        self._started = False

    def recover(self) -> list[str]:
        """Requeue every non-terminal task and say so in the stream."""
        recovered = self.store.recover_interrupted()
        for task_id in recovered:
            task = self.store.get(task_id)
            self.bus.publish(
                task_id,
                "task.recovered",
                {
                    "reason": "runtime restarted while the task was in flight",
                    "previous_state": (task.result or {}).get("previous_state"),
                },
            )
        return recovered

    # ---------------------------------------------------------- submit
    def submit(self, request: TaskRequest, project_id: Optional[str] = None) -> TaskRecord:
        """Validate, persist, enqueue. Returns immediately."""
        repo = self._validate_repo(request.repo)
        task = TaskRecord(
            goal=request.goal,
            repo=repo,
            project_id=project_id or request.project_id,
            preferences=dict(request.preferences or {}),
            planner=self.architect.name,
            timeout_seconds=request.timeout_seconds or self.timeout_seconds,
            state=TaskState.queued,
            progress=0,
            current_step="queued",
        )
        self.store.create(task)
        self.bus.publish(
            task.id,
            "task.created",
            {
                "goal": task.goal,
                "repo": task.repo,
                "planner": task.planner,
                "preferences": task.preferences,
            },
        )
        self.enqueue(task.id)
        return task

    def enqueue(self, task_id: str) -> None:
        self._queue.put(task_id)

    def _validate_repo(self, repo: Optional[str]) -> Optional[str]:
        if not repo:
            raise ValueError("repo is required for background tasks")
        canonical = canonical_repo_path(repo)
        if not canonical:
            raise ValueError("repo path is empty")
        path = Path(canonical)
        if not path.exists() or not path.is_dir():
            raise ValueError(f"repo path does not exist: {canonical}")
        if not (path / ".git").exists():
            raise ValueError(f"repo path is not a git repository: {canonical}")
        try:
            return ensure_authorized(canonical)
        except RepoNotAllowed as exc:
            raise ValueError(str(exc)) from exc

    # -------------------------------------------------------- workers
    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                task_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._execute(task_id)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("task %s crashed in the engine: %s", task_id, exc)
                self._fail(
                    task_id,
                    {"type": type(exc).__name__, "message": str(exc)},
                    "engine exception",
                )
            finally:
                with self._lock:
                    self._running.discard(task_id)
                self._queue.task_done()

    def _execute(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None:
            return
        if task.cancel_requested or task.state in {
            TaskState.completed,
            TaskState.failed,
            TaskState.cancelled,
            TaskState.blocked,
        }:
            if task.cancel_requested and task.state not in {
                TaskState.cancelled,
                TaskState.completed,
            }:
                self._finalize_cancelled(task_id, "cancelled before start")
            return

        with self._lock:
            self._running.add(task_id)

        deadline = time.monotonic() + float(task.timeout_seconds or self.timeout_seconds)
        cancel = self._cancel_checker(task_id, deadline)
        heartbeat = _Heartbeat(self.store, task_id, HEARTBEAT_SECONDS)

        worktree: Optional[Path] = None
        try:
            # ---------------------------------------------- PLANNING
            self._transition(task_id, TaskState.planning, "planner analyzing repository")
            self._emit(task_id, "planner.started", planner=self.architect.name)
            try:
                plan, backend, reason = self.architect.plan(
                    task.goal,
                    task.repo,
                    previous_evidence=task.result.get("evidence") if task.result else None,
                    cancel_check=cancel,
                )
            except PlannerError as exc:
                self._emit(
                    task_id,
                    "planner.failed",
                    error={"type": "PlannerError", "message": str(exc)},
                    attempts=[
                        {"backend": a.backend, "error": a.error}
                        for a in self.architect.attempts
                    ],
                )
                self._fail(
                    task_id,
                    {
                        "type": "PlannerError",
                        "message": str(exc),
                        "attempts": [
                            {"backend": a.backend, "error": a.error}
                            for a in self.architect.attempts
                        ],
                    },
                    "planner output was invalid",
                    state=TaskState.blocked,
                )
                return

            task = self.store.update(
                task_id,
                plan=plan,
                planner_backend=backend,
                result={
                    **(task.result or {}),
                    "planner_reason": reason,
                    "planner_attempts": [
                        {"backend": a.backend, "error": a.error}
                        for a in self.architect.attempts
                    ],
                },
            ) or task
            self._emit(
                task_id,
                "planner.completed",
                plan=plan.model_dump(),
                backend=backend,
            )

            # --------------------------------------- EXECUTOR SELECT
            # Health-gated: the chain contains only executors that are
            # eligible right now, in preference order. The engine may
            # attempt up to MAX_EXECUTOR_ATTEMPTS distinct runs; each
            # failure is classified and, when the evidence says the
            # executor itself is broken, the circuit opens and the next
            # eligible executor takes over with a fresh worktree.
            try:
                chain, choice_reason = self.registry.select_chain(
                    plan, preference=(task.preferences or {}).get("executor")
                )
            except RuntimeError as exc:
                self._fail(
                    task_id,
                    {"type": "NoExecutorAvailable", "message": str(exc)},
                    "no coding executor is available",
                    state=TaskState.blocked,
                )
                return
            if not chain:
                self._fail(
                    task_id,
                    {
                        "type": "NoExecutorEligible",
                        "message": choice_reason,
                    },
                    "no healthy executor is eligible",
                    state=TaskState.blocked,
                )
                return

            # ------------------------------------ EXECUTION SOURCE
            # Background tasks must not depend on the user's working
            # copy being clean -- and must never fix it. A clean repo
            # is used directly; a dirty one is served from a managed
            # mirror refreshed read-only from the user's HEAD.
            source_info = resolve_execution_source(task.repo)
            if source_info.get("error"):
                error = dict(source_info["error"])
                self._fail(
                    task_id,
                    error,
                    "could not establish a clean execution source",
                    state=TaskState.blocked,
                )
                return
            exec_repo = Path(source_info["repo"])
            if source_info.get("mirror"):
                task = self.store.update(
                    task_id,
                    result={
                        **(task.result or {}),
                        "execution_source": {
                            "repo": source_info["repo"],
                            "source_repo": source_info.get("source_repo"),
                            "source_head": source_info.get("source_head"),
                            "mirror": True,
                        },
                    },
                ) or task
                self._emit(
                    task_id,
                    "source.mirrored",
                    execution_repo=source_info["repo"],
                    source_repo=source_info.get("source_repo"),
                    source_head=source_info.get("source_head"),
                )

            max_attempts = MAX_EXECUTOR_ATTEMPTS
            # Transient-class failures earn one same-executor retry:
            # each chain entry appears twice, and the attempt bound
            # still caps total work. Fatal-class failures add their
            # executor to seen_fatal so it is never re-entered.
            expanded: list[str] = []
            for entry in chain:
                expanded.extend([entry, entry])
            seen_fatal: set[str] = set()
            attempts_history: list[dict[str, Any]] = []
            executor_id: Optional[str] = None
            executor = None
            outcome = None
            attempt_no = 0

            for candidate_id in expanded:
                if attempt_no >= max_attempts:
                    break
                if candidate_id in seen_fatal:
                    continue
                candidate = self.registry.get(candidate_id)
                if candidate is None:  # pragma: no cover - registry invariant
                    continue
                executor_id = candidate_id
                executor = candidate
                attempt_no += 1
                attempt_started = _now()

                # The circuit may have opened since the chain was built
                # (another task's failure). Never enter a known-broken
                # executor.
                health = self.registry.health_of(executor_id)
                if health is not None and not health.eligible:
                    attempts_history.append(
                        {
                            "executor": executor_id,
                            "attempt": attempt_no,
                            "skipped": True,
                            "reason": (
                                f"health gate: {health.error_type or 'ineligible'}"
                            ),
                            "started_at": attempt_started,
                        }
                    )
                    self._emit(
                        task_id,
                        "executor.fallback",
                        executor=executor_id,
                        reason=(
                            f"health gate rejected {executor_id}: "
                            f"{health.error_type or 'ineligible'}"
                        ),
                    )
                    attempt_no -= 1
                    continue

                task = self.store.update(
                    task_id,
                    executor=executor_id,
                    executor_reason=choice_reason,
                    state=TaskState.planned,
                    progress=20,
                    current_step=f"executor: {executor.label}",
                    result={
                        **(task.result or {}),
                        "executor_attempts": attempts_history,
                    },
                ) or task
                self._emit(
                    task_id,
                    "executor.selected",
                    executor=executor_id,
                    label=executor.label,
                    reason=choice_reason,
                    attempt=attempt_no,
                    chain=list(chain),
                )

                # ----------------------------------------- PREPARING
                cancel()
                self._transition(
                    task_id, TaskState.preparing, "allocating isolated worktree"
                )
                self.worktree_root.mkdir(parents=True, exist_ok=True)
                allocation = worktree_guard.allocate(
                    exec_repo,
                    self.worktree_root,
                    run=run_command,
                )
                if allocation.get("error"):
                    error_type = allocation["error"].get("type", "")
                    if error_type == "DirtyRepo":
                        # The user's working copy is not ours to fix:
                        # no clean, no reset, no stash. Say so and stop.
                        self._fail(
                            task_id,
                            dict(allocation["error"]),
                            "source repository has uncommitted changes; "
                            "point the task at a clean managed clone",
                            state=TaskState.blocked,
                        )
                    else:
                        self._fail(
                            task_id,
                            dict(allocation["error"]),
                            "worktree allocation failed",
                        )
                    return
                worktree = Path(allocation["worktree"])
                task = self.store.update(
                    task_id,
                    worktree=str(worktree),
                    branch=allocation["branch"],
                    base_sha=allocation["base_sha"],
                ) or task
                self._emit(
                    task_id,
                    "worktree.created",
                    worktree=str(worktree),
                    branch=allocation["branch"],
                    base_sha=allocation["base_sha"],
                    attempt=attempt_no,
                )

                # ------------------------------------------- CODING
                self._transition(
                    task_id, TaskState.coding, f"{executor.label} editing files"
                )
                self._emit(
                    task_id,
                    "executor.started",
                    executor=executor_id,
                    worktree=str(worktree),
                    attempt=attempt_no,
                )

                sink = _OutputSink(self.store, self.bus, task_id)
                ctx = ExecutionContext(
                    task=task,
                    repo=exec_repo,
                    worktree=worktree,
                    plan=plan,
                    timeout_seconds=max(30.0, deadline - time.monotonic()),
                    cancel_check=cancel,
                    on_output=sink.push,
                    on_pid=lambda pid: self._set_pid(task_id, pid),
                )
                attempt_start_mono = time.monotonic()
                try:
                    outcome = executor.execute(ctx)
                finally:
                    sink.flush()
                    self._set_pid(task_id, None)

                self._emit(
                    task_id,
                    "executor.completed",
                    executor=executor_id,
                    succeeded=outcome.succeeded,
                    exit_code=outcome.exit_code,
                    summary=outcome.summary,
                    files_touched=outcome.files_touched,
                    attempt=attempt_no,
                )

                changed = _changed_files(worktree)
                if changed:
                    self._emit(task_id, "file.changed", files=changed)
                task = self.store.update(
                    task_id,
                    files_changed=len(changed),
                    touched_files=changed,
                ) or task

                if outcome.succeeded:
                    self.registry.health_service.record_success(executor_id)
                    # Record the successful attempt as well: the history
                    # must show which executor actually finished the
                    # task, how long it took, and what it changed -- not
                    # only the attempts that failed.
                    attempts_history.append(
                        {
                            "executor": executor_id,
                            "attempt": attempt_no,
                            "started_at": attempt_started,
                            "finished_at": _now(),
                            "duration_seconds": round(
                                time.monotonic() - attempt_start_mono, 2
                            ),
                            "exit_code": outcome.exit_code,
                            "error_type": None,
                            "action": "SUCCESS",
                            "error": None,
                            "worktree": str(worktree),
                            "changed_files": len(changed),
                            "fallback_reason": None,
                        }
                    )
                    task = self.store.update(
                        task_id,
                        result={
                            **(task.result or {}),
                            "executor_attempts": attempts_history,
                        },
                    ) or task
                    break

                # ------------------------------------- FAILURE ROUTING
                error_dict = dict(outcome.error or {})
                message = str(
                    error_dict.get("message")
                    or outcome.summary
                    or "executor failed"
                )
                raw_status = error_dict.get("api_error_status") or error_dict.get("status")
                status_int = (
                    int(raw_status)
                    if isinstance(raw_status, (int, str)) and str(raw_status).isdigit()
                    else None
                )
                error_type = self.registry.health_service.record_failure(
                    executor_id, message, status_int
                )
                action = error_class_action(error_type)
                attempt_record = {
                    "executor": executor_id,
                    "attempt": attempt_no,
                    "started_at": attempt_started,
                    "finished_at": _now(),
                    "duration_seconds": round(
                        time.monotonic() - attempt_start_mono, 2
                    ),
                    "exit_code": outcome.exit_code,
                    "error_type": error_type,
                    "action": action,
                    "error": message[:500],
                    "worktree": str(worktree),
                    "changed_files": len(changed),
                    "fallback_reason": None,
                }
                attempts_history.append(attempt_record)
                task = self.store.update(
                    task_id,
                    result={
                        **(task.result or {}),
                        "executor_attempts": attempts_history,
                    },
                ) or task
                self._emit(
                    task_id,
                    "executor.failed",
                    executor=executor_id,
                    attempt=attempt_no,
                    error_type=error_type,
                    action=action,
                    error=message[:500],
                )

                if action == "NON_RETRYABLE_TASK_ERROR":
                    self._fail(
                        task_id,
                        {
                            "type": "ExecutorFailed",
                            "executor": executor_id,
                            "error_type": error_type,
                            "message": message,
                            "attempts": attempts_history,
                        },
                        "executor failed with a non-retryable task error",
                        extra={
                            "executor": executor_id,
                            "summary": outcome.summary,
                        },
                    )
                    return

                # Fatal-for-executor: the circuit is now open
                # (record_failure did that) and this executor is never
                # re-entered for this task. Transient: the duplicate
                # chain entry below gives it one same-executor retry.
                self._cleanup_worktree(task, worktree, failed=True)
                worktree = None
                if action == "RETRY_DIFFERENT_EXECUTOR":
                    seen_fatal.add(executor_id)
                next_candidate = next(
                    (
                        c
                        for c in expanded[expanded.index(candidate_id) + 1:]
                        if c not in seen_fatal
                    ),
                    None,
                )
                attempt_record["fallback_reason"] = (
                    f"{error_type} -> "
                    + (
                        f"retry with {next_candidate}"
                        if next_candidate
                        else "no executor left"
                    )
                )
                task = self.store.update(
                    task_id,
                    result={
                        **(task.result or {}),
                        "executor_attempts": attempts_history,
                    },
                ) or task
                self._emit(
                    task_id,
                    "executor.fallback",
                    executor=executor_id,
                    attempt=attempt_no,
                    error_type=error_type,
                    next_executor=next_candidate,
                    reason=attempt_record["fallback_reason"],
                )
                outcome = None

            if outcome is None or not outcome.succeeded or executor_id is None:
                self._fail(
                    task_id,
                    {
                        "type": "AllExecutorsFailed",
                        "message": (
                            f"all executor attempts failed after {attempt_no} "
                            "attempt(s)"
                        ),
                        "attempts": attempts_history,
                    },
                    "every eligible executor attempt failed",
                    extra={"attempts": attempts_history},
                )
                return

            # ----------------------------------------------- TESTING
            cancel()
            self._transition(task_id, TaskState.testing, "running repository tests")
            verifier = self.verifier_factory(
                cancel_check=cancel,
                on_event=lambda kind, payload: self._emit(task_id, kind, **payload),
            )
            suite = verifier.run_suite(worktree, plan, timeout=self._suite_timeout(deadline))
            task = self.store.update(
                task_id,
                tests_passed=suite["tests_passed"],
                tests_failed=suite["tests_failed"],
                build_status=suite["build_status"],
            ) or task

            # --------------------------------------------- VERIFYING
            cancel()
            self._transition(task_id, TaskState.verifying, "verifying real evidence")
            self._emit(task_id, "verification.started", phase="pre-commit")
            report = verifier.verify_worktree(
                task,
                plan,
                exec_repo,
                worktree,
                timeout=self._suite_timeout(deadline),
                suite=suite,
            )
            self._emit(
                task_id,
                "verification.completed",
                phase="pre-commit",
                status=report.status,
                checks=[
                    {"name": c.name, "status": c.status, "detail": c.detail}
                    for c in report.checks
                ],
            )
            if report.status != "PASS":
                self._record_verification(task_id, report)
                self._fail(
                    task_id,
                    {
                        "type": "VerificationFailed",
                        "message": "pre-commit verification failed",
                        "failed_checks": [
                            {"name": c.name, "detail": c.detail}
                            for c in report.failed_checks()
                        ],
                    },
                    "verification failed",
                    retain_worktree=True,
                )
                return

            # -------------------------------------------- COMMITTING
            cancel()
            self._transition(task_id, TaskState.committing, "committing verified change")
            commit = self._commit(task, worktree)
            if commit.get("error"):
                self._record_verification(task_id, report)
                self._fail(
                    task_id,
                    dict(commit["error"]),
                    "commit failed",
                    retain_worktree=True,
                )
                return

            commit_check = verifier.verify_commit(
                task,
                plan,
                exec_repo,
                worktree,
                commit["sha"],
                expected_files=_expected_files(plan),
            )
            report = verifier.merge(report, commit_check)
            self._record_verification(task_id, report)
            if report.status != "PASS":
                self._fail(
                    task_id,
                    {
                        "type": "VerificationFailed",
                        "message": "commit verification failed",
                        "failed_checks": [
                            {"name": c.name, "detail": c.detail}
                            for c in report.failed_checks()
                        ],
                    },
                    "commit verification failed",
                    retain_worktree=True,
                )
                return

            self._emit(
                task_id,
                "git.committed",
                commit=commit["sha"],
                branch=commit["branch"],
                message=commit["message"],
                files=list(report.touched_files or []),
            )

            # -------------------------------------------- COMPLETED
            # Order matters: the terminal event is committed BEFORE the
            # state flips to terminal, so no observer can ever see a
            # COMPLETED task whose task.completed event is missing.
            final = self.store.update(
                task_id,
                commit_sha=commit["sha"],
                verify_status="PASS",
                files_changed=report.files_changed,
                touched_files=report.touched_files,
                result={
                    **(self.store.get(task_id).result or {}),
                    "summary": plan.summary,
                    "executor": executor_id,
                    "planner_backend": backend,
                    "commit": commit["sha"],
                    "branch": commit["branch"],
                },
            )
            self._emit(
                task_id,
                "task.completed",
                commit=commit["sha"],
                branch=commit["branch"],
                files_changed=report.files_changed,
                tests_passed=report.tests_passed,
                tests_failed=report.tests_failed,
                build_status=report.build_status,
                duration_seconds=final.elapsed_seconds() if final else 0,
            )
            self._transition(task_id, TaskState.completed, "completed", progress=100)
            self.completed_count += 1
            self._cleanup_worktree(task, worktree, failed=False)

        except (TaskCancelled, MissionCancelled):
            self._finalize_cancelled(task_id, "cancelled during execution")
            self._cleanup_worktree(task, worktree, failed=True)
        except TaskTimeout as exc:
            self._fail(
                task_id,
                {"type": "Timeout", "message": str(exc)},
                "task exceeded its time budget",
                state=TaskState.failed,
            )
            self._cleanup_worktree(task, worktree, failed=True)
        except Exception as exc:
            logger.exception("task %s failed: %s", task_id, exc)
            self._fail(
                task_id,
                {"type": type(exc).__name__, "message": str(exc)},
                "unexpected engine failure",
            )
            self._cleanup_worktree(task, worktree, failed=True)
        finally:
            heartbeat.stop()

    # -------------------------------------------------------- helpers
    def _commit(self, task: TaskRecord, worktree: Path) -> dict:
        run_command(["git", "add", "-A"], cwd=str(worktree), timeout=120)
        staged = run_command(["git", "diff", "--cached", "--name-only"], cwd=str(worktree))
        if not (staged.get("stdout") or "").strip():
            return {
                "error": {
                    "type": "NoChange",
                    "message": "nothing was staged; nothing to commit",
                }
            }

        message = f"kodgar: {task.goal.strip().splitlines()[0][:68]}"
        result = run_command(
            ["git", "commit", "-m", message], cwd=str(worktree), timeout=180
        )
        if result.get("returncode") != 0:
            return {
                "error": {
                    "type": "CommitError",
                    "message": (
                        (result.get("stderr") or "") + (result.get("stdout") or "")
                    )[:500],
                }
            }
        sha_result = run_command(["git", "rev-parse", "HEAD"], cwd=str(worktree), timeout=60)
        sha = (sha_result.get("stdout") or "").strip()
        if not sha:
            return {
                "error": {
                    "type": "CommitError",
                    "message": "commit produced no resolvable HEAD",
                }
            }
        return {"sha": sha, "branch": task.branch, "message": message}

    def cancel(self, task_id: str) -> str:
        outcome = self.store.request_cancel(task_id)
        if outcome in {"cancelled", "requested"}:
            with self._lock:
                running = task_id in self._running
            self.bus.publish(
                task_id,
                "task.cancel_requested",
                {"running": running, "outcome": outcome},
            )
            if outcome == "cancelled":
                self._transition(task_id, TaskState.cancelled, "cancelled", progress=100)
                self._emit(task_id, "task.cancelled", reason="cancelled before start")
        return outcome

    def _cancel_checker(self, task_id: str, deadline: float):
        def check() -> None:
            if self.store.cancel_requested(task_id):
                raise TaskCancelled(f"task {task_id} was cancelled")
            if time.monotonic() >= deadline:
                raise TaskTimeout(f"task {task_id} exceeded its time budget")

        return check

    def _suite_timeout(self, deadline: float) -> float:
        """Bounded test/build budget for one verification pass.

        The cap must comfortably fit real repositories: the Kodgar
        core suite alone takes ~15 minutes on a busy developer
        machine. It is configurable but never extends past the task's
        own deadline, so a hung suite still gets killed.
        """
        cap = float(os.environ.get("KODGAR_SUITE_TIMEOUT", "1500"))
        return max(30.0, min(cap, deadline - time.monotonic()))

    def _transition(
        self, task_id: str, state: TaskState, step: str, progress: Optional[int] = None
    ) -> None:
        self.store.transition(task_id, state, step, progress)
        task = self.store.get(task_id)
        self._emit(
            task_id,
            "task.state",
            state=state.value,
            progress=task.progress if task else 0,
            current_step=step,
        )

    def _emit(self, task_id: str, event_type: str, **payload: Any) -> None:
        try:
            self.bus.publish(task_id, event_type, payload)
        except Exception:  # pragma: no cover - events must never break a task
            logger.exception("failed to emit %s for %s", event_type, task_id)

    def _set_pid(self, task_id: str, pid: Optional[int]) -> None:
        try:
            self.store.update(task_id, pid=pid)
        except Exception:  # pragma: no cover
            pass
        self._emit(task_id, "executor.pid", pid=pid)

    def _record_verification(self, task_id: str, report) -> None:
        self.store.update(
            task_id,
            verification=report,
            verify_status=report.status,
            files_changed=report.files_changed,
            touched_files=report.touched_files,
            tests_passed=report.tests_passed,
            tests_failed=report.tests_failed,
            build_status=report.build_status,
        )

    def _fail(
        self,
        task_id: str,
        error: dict,
        reason: str,
        state: TaskState = TaskState.failed,
        extra: Optional[dict] = None,
        retain_worktree: bool = False,
    ) -> None:
        current = self.store.get(task_id)
        self.store.update(
            task_id,
            error=error,
            result={**(current.result if current else {}), **(extra or {})},
        )
        # Terminal event commits before the terminal state flip (same
        # invariant as the COMPLETED path: terminal state never becomes
        # visible without its terminal event already in the store).
        event = (
            "task.blocked" if state == TaskState.blocked else "task.failed"
        )
        self._emit(task_id, event, reason=reason, error=error)
        self.store.transition(task_id, state, reason, progress=None)
        if state not in {TaskState.completed, TaskState.cancelled}:
            self.failed_count += 1

    def _finalize_cancelled(self, task_id: str, reason: str) -> None:
        current = self.store.get(task_id)
        if current is None or current.state == TaskState.cancelled:
            return
        # Terminal event first, then the state flip (see _fail).
        self._emit(task_id, "task.cancelled", reason=reason)
        self.store.transition(task_id, TaskState.cancelled, reason, progress=100)

    def _cleanup_worktree(
        self, task: TaskRecord, worktree: Optional[Path], failed: bool
    ) -> None:
        if worktree is None:
            return
        keep = self.keep_worktrees or failed
        # Worktrees are registered against the *execution source*
        # (which may be a managed mirror), so cleanup must target the
        # same repository git metadata lives in.
        source_meta = (task.result or {}).get("execution_source") or {}
        cleanup_repo = Path(source_meta.get("repo") or task.repo)
        try:
            worktree_guard.cleanup(
                cleanup_repo, worktree, keep=keep, failed=failed, run=run_command
            )
        except Exception:  # pragma: no cover - cleanup is best effort
            logger.exception("worktree cleanup failed for %s", task.id)

    # --------------------------------------------------------- status
    def status(self) -> dict:
        return {
            "started": self._started,
            "started_at": self.started_at,
            "workers": self.worker_count,
            "queued": self._queue.qsize(),
            "running": sorted(self._running),
            "completed": self.completed_count,
            "failed": self.failed_count,
            "executors": self.registry.status(),
        }


class _OutputSink:
    """Batches executor output into durable logs and live events."""

    def __init__(self, store: TaskStore, bus: TaskEventBus, task_id: str):
        self.store = store
        self.bus = bus
        self.task_id = task_id
        self._buffer: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

    def push(self, stream: str, line: str) -> None:
        with self._lock:
            self._buffer.append((stream, line))
            due = (
                len(self._buffer) >= LOG_FLUSH_LINES
                or time.monotonic() - self._last_flush >= LOG_FLUSH_SECONDS
            )
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            batch = self._buffer
            self._buffer = []
            self._last_flush = time.monotonic()
        by_stream: dict[str, list[str]] = {}
        for stream, line in batch:
            by_stream.setdefault(stream, []).append(line)
        for stream, lines in by_stream.items():
            self.store.append_log(self.task_id, lines, stream=stream)
        self.bus.publish(
            self.task_id,
            "executor.output",
            {
                "lines": [
                    {"stream": stream, "line": line} for stream, line in batch[:200]
                ]
            },
        )


class _Heartbeat:
    """Keeps ``heartbeat_at`` fresh so a stalled task is visible."""

    def __init__(self, store: TaskStore, task_id: str, interval: int):
        self.store = store
        self.task_id = task_id
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.store.update(self.task_id, heartbeat_at=_now())
            except Exception:  # pragma: no cover
                return

    def stop(self) -> None:
        self._stop.set()


def _changed_files(worktree: Path) -> list[str]:
    result = run_command(["git", "status", "--porcelain"], cwd=str(worktree), timeout=60)
    files: list[str] = []
    for line in (result.get("stdout") or "").splitlines():
        entry = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in entry:
            entry = entry.split(" -> ")[-1].strip()
        if entry:
            files.append(entry.strip('"'))
    return files


def _expected_files(plan: PlanResult) -> list[str]:
    expected = set(plan.target_files)
    for edit in plan.edits:
        expected.add(edit.target_file)
    return sorted(expected)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _now() -> str:
    from app.background.models import now_iso

    return now_iso()
