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
            timeout_seconds=self.timeout_seconds,
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
            try:
                executor_id, choice_reason = self.registry.select(
                    plan, preference=(task.preferences or {}).get("executor")
                )
            except RuntimeError as exc:
                # No coding agent exists on this machine at all: that is
                # an environment problem, not a task failure.
                self._fail(
                    task_id,
                    {"type": "NoExecutorAvailable", "message": str(exc)},
                    "no coding executor is available",
                    state=TaskState.blocked,
                )
                return
            executor = self.registry.get(executor_id)
            if executor is None:  # pragma: no cover - registry invariant
                raise RuntimeError(f"executor {executor_id} vanished from the registry")

            task = self.store.update(
                task_id,
                executor=executor_id,
                executor_reason=choice_reason,
                state=TaskState.planned,
                progress=20,
                current_step=f"executor: {executor.label}",
            ) or task
            self._emit(
                task_id,
                "executor.selected",
                executor=executor_id,
                label=executor.label,
                reason=choice_reason,
            )

            # --------------------------------------------- PREPARING
            cancel()
            self._transition(
                task_id, TaskState.preparing, "allocating isolated worktree"
            )
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            allocation = worktree_guard.allocate(
                Path(task.repo),
                self.worktree_root,
                branch_name=task.result.get("branch_name") if task.result else None,
                run=run_command,
            )
            if allocation.get("error"):
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
            )

            # ------------------------------------------------ CODING
            self._transition(
                task_id, TaskState.coding, f"{executor.label} editing files"
            )
            self._emit(
                task_id,
                "executor.started",
                executor=executor_id,
                worktree=str(worktree),
            )

            sink = _OutputSink(self.store, self.bus, task_id)
            ctx = ExecutionContext(
                task=task,
                repo=Path(task.repo),
                worktree=worktree,
                plan=plan,
                timeout_seconds=max(30.0, deadline - time.monotonic()),
                cancel_check=cancel,
                on_output=sink.push,
                on_pid=lambda pid: self._set_pid(task_id, pid),
            )
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
            )

            changed = _changed_files(worktree)
            if changed:
                self._emit(task_id, "file.changed", files=changed)
            task = self.store.update(
                task_id,
                files_changed=len(changed),
                touched_files=changed,
            ) or task

            if not outcome.succeeded:
                self._fail(
                    task_id,
                    dict(outcome.error or {"type": "ExecutorFailed", "message": outcome.summary}),
                    "executor failed",
                    extra={"executor": executor_id, "summary": outcome.summary},
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
                Path(task.repo),
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
                Path(task.repo),
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
        return max(30.0, min(600.0, deadline - time.monotonic()))

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
        try:
            worktree_guard.cleanup(
                Path(task.repo), worktree, keep=keep, failed=failed, run=run_command
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
