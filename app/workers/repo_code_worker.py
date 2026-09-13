import os
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from app.runtime.repo_identity import RepoNotAllowed, ensure_authorized
from app.workers.base import Worker, WorkerResult
from app.workers import (
    diff_guard,
    edit_engine,
    safe_subprocess,
    validation,
    worktree_guard,
)
from app.workers.mission_evidence import now_iso
from app.workers.worker_errors import (
    MissionCancelled,
    MissionTimeout,
    ToolMissingError,
)
from app.llm.coder import generate_edit_plan, generate_repair_plan
from app.learning.retrieval import (
    format_lessons,
    retrieve_relevant_learnings,
)
from app.llm.provider import pop_attempt_log

# Edit engine is the single source of truth for every file mutation.
read_text_preserve = edit_engine.read_text_preserve
write_text_preserve = edit_engine.write_text_preserve
find_relaxed_unique_match = edit_engine.find_relaxed_unique_match
normalize_edits = edit_engine.normalize_edits
prepare_edits = edit_engine.prepare_edits
apply_edits = edit_engine.apply_edits
restore_originals = edit_engine.restore_originals

class CancelContext:
    """
    Stage 8.4/8.8: cancellation, deadline, and event context for one
    executing mission.

    cancel_check() raises MissionCancelled when cancellation was
    requested and MissionTimeout when the mission deadline expired;
    the worker calls it at every checkpoint (before LLM calls,
    before applying edits, before validation, between repair
    attempts, before commit) so a cancelled or timed-out mission
    never commits and never keeps running.

    emit() persists structured mission events for observability;
    it must never break mission execution.
    """

    def __init__(self, mission_id: Optional[str], store=None, timeout_seconds=None):
        self.mission_id = mission_id
        self.store = store
        self.cancel_requested = False
        self.deadline = None
        if timeout_seconds:
            try:
                self.deadline = time.monotonic() + float(timeout_seconds)
            except (TypeError, ValueError):
                self.deadline = None
        if mission_id and store is not None:
            try:
                existing = store.get(mission_id)
                if existing is not None:
                    self.cancel_requested = bool(existing.cancel_requested)
            except Exception:
                pass

    def bind(self, store, mission_id: Optional[str]):
        self.store = store

        if mission_id and mission_id != self.mission_id:
            self.mission_id = mission_id

            if store is not None:
                try:
                    existing = store.get(mission_id)
                except Exception:
                    existing = None

                if existing is not None:
                    self.cancel_requested = bool(
                        existing.cancel_requested
                    )

    def refresh(self):
        if self.store is None or self.mission_id is None:
            return

        try:
            mission = self.store.get(self.mission_id)
        except Exception:
            return

        if mission is not None:
            self.cancel_requested = bool(mission.cancel_requested)

    def cancel_check(self, at: str = ""):
        self.refresh()

        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise MissionTimeout(
                "mission exceeded its deadline at %s"
                % (at or "checkpoint")
            )

        if self.cancel_requested:
            raise MissionCancelled(at or "checkpoint")

    def emit(self, event_type: str, attempt: int = 0, **data):
        if self.store is None or self.mission_id is None:
            return

        try:
            self.store.record_event(
                self.mission_id,
                event_type,
                attempt=attempt,
                data=data or None,
            )
        except Exception:
            # Observability must never break execution.
            pass


class _MissionRuntime:
    """Minimal per-mission runtime bound to the calling thread.

    Kept thread-local so the long-lived worker functions (`run`,
    `run_validation`) pick up the mission's deadline and
    cancellation without changing their legacy signatures, which
    existing tests and monkeypatch seams depend on.
    """

    __slots__ = ("cancel_check", "deadline")

    def __init__(self, cancel_check, deadline):
        self.cancel_check = cancel_check
        self.deadline = deadline


_local = threading.local()


@contextmanager
def _mission_runtime(ctx: CancelContext):
    previous = getattr(_local, "runtime", None)
    _local.runtime = _MissionRuntime(ctx.cancel_check, ctx.deadline)
    try:
        yield
    finally:
        _local.runtime = previous


def provider_attempt_evidence() -> list[dict]:
    """Stage 8.6: surface provider attempts as evidence."""
    attempts = pop_attempt_log()

    if not attempts:
        return []

    return [
        {
            "type": "provider_attempts",
            "attempts": attempts,
            "timestamp": now_iso(),
        }
    ]


def which(tool: str):
    return shutil.which(tool)


DEFAULT_COMMAND_TIMEOUT = 300


def run(cmd, cwd=None, timeout=DEFAULT_COMMAND_TIMEOUT, env=None):
    """Run one command as one bounded, cancellable unit.

    Inside a mission (thread-local runtime bound) the mission's
    deadline and cancellation apply to the whole process group:
    timeout or cancellation kills the tree before any exception
    propagates, and a deadline-expiring run raises MissionTimeout so
    no commit path can ever observe a timed-out result as success.
    Outside a mission this is a plain bounded subprocess run with
    the same result shape.

    The signature (cmd, cwd, timeout) is a legacy test seam;
    monkeypatched runners in the suite depend on it.
    """
    runtime = getattr(_local, "runtime", None)

    if runtime is None:
        return safe_subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env)

    effective = timeout
    deadline_bound = False

    if runtime.deadline is not None:
        remaining = runtime.deadline - time.monotonic()

        if remaining <= 0:
            raise MissionTimeout(
                "mission deadline already exceeded before: %s" % cmd
            )

        if remaining < timeout:
            effective = remaining
            deadline_bound = True

    result = safe_subprocess.run(
        cmd,
        cwd=cwd,
        timeout=effective,
        cancel_check=runtime.cancel_check,
        env=env,
    )

    if result.get("timed_out") and deadline_bound:
        raise MissionTimeout(
            "mission exceeded its deadline during: %s"
            % result.get("cmd", cmd)
        )

    return result


def detect_test_commands(worktree: Path):
    """Language-aware validation detection (validation module).

    Delegates with the worker's own `which` so the existing
    monkeypatch seam on this module keeps working.
    """
    return validation.detect_test_commands(worktree, tool_check=which)


def run_validation(
    worktree: Path,
    evidence: list,
    test_commands: list,
):
    """
    Run every detected test command once and return
    (passed, results).

    - Each command runs bounded (1MB output cap) through `run`,
      so the mission deadline and cancellation apply mid-command.
    - A selected validation tool must exist; a missing executable
      is an explicit ToolMissingError, never a silent skip.
    - No test commands => not passed: an empty validation plan
      must never produce a vacuous pass.
    """
    results = []

    for cmd in test_commands:
        try:
            result = run(cmd, cwd=worktree)
        except FileNotFoundError as exc:
            # A selected validation tool must exist; fail with a
            # clear environment diagnostic, never a silent one.
            raise ToolMissingError(
                f"Validation tool not executable: {cmd[0]} ({exc})"
            ) from exc

        evidence.append(result)
        results.append(result)

    passed = all(
        item.get("returncode") == 0 and not item.get("timed_out")
        for item in results
    ) if results else False

    return passed, results


def build_failure_context(
    attempt: int,
    test_results: list,
    diff_text: str,
    touched_files: list,
) -> dict:
    return {
        "attempt": attempt,
        "tests": test_results,
        "diff": diff_text,
        "touched_files": touched_files,
    }


def collect_learning_lessons(
    goal: str,
    evidence: list,
    mission_id: Optional[str] = None,
    limit: int = 3,
):
    """
    Retrieve relevant prior learnings for planning context and
    record retrieval evidence. Retrieval is advisory guidance and
    must never corrupt the mission.
    """
    try:
        records = retrieve_relevant_learnings(goal, limit=limit)
    except Exception as exc:
        evidence.append(
            {
                "type": "learning_retrieval",
                "mission_id": mission_id,
                "records_used": [],
                "count": 0,
                "error": str(exc),
                "timestamp": now_iso(),
            }
        )
        return ""

    evidence.append(
        {
            "type": "learning_retrieval",
            "mission_id": mission_id,
            "records_used": [
                {
                    "id": record.id,
                    "mission_id": record.mission_id,
                    "outcome": record.outcome,
                    "goal": record.goal,
                }
                for record in records
            ],
            "count": len(records),
            "timestamp": now_iso(),
        }
    )

    if not records:
        return ""

    return "\n".join(format_lessons(records))


def cleanup_worktree(
    worktree: Path,
    repo: Path,
    keep_worktree: bool,
    evidence: list,
    failed: bool,
):
    """
    Safe worktree lifecycle cleanup (worktree guard policy).

    Policy:
    - keep_worktree=True keeps the worktree for any outcome.
    - failed missions keep their worktree explicitly for
      debugging/evidence (recorded, never silent).
    - successful missions remove the worktree and prune.

    Cleanup failure must never hide the original mission result;
    it is recorded as worktree_cleanup_error evidence instead.
    """
    worktree_guard.cleanup(
        repo,
        worktree,
        keep=keep_worktree,
        failed=failed,
        run=run,
        evidence=evidence,
    )


def worktree_is_clean(worktree: Path) -> bool:
    return worktree_guard.worktree_is_clean(worktree, run=run)


# -------------------------------------------------------------
# evidence_report: one consistent terminal report per outcome.
# -------------------------------------------------------------

EVENT_DIFF_LIMIT = 8 * 1024
EVENT_COMMANDS_LIMIT = 200
_EVENT_WARNINGS_LIMIT = 100

_TEST_RUNNER_MARKERS = (
    "pytest",
    "npm test",
    "go test",
    "cargo test",
    "mvn",
    "gradle",
    "dotnet",
    "swift test",
    "ctest",
    "make test",
    "bash -n",
    "sh -n",
    "sqlite3",
)


def _run_results(evidence: list) -> list:
    return [
        item
        for item in evidence
        if isinstance(item, dict)
        and isinstance(item.get("returncode"), int)
        and item.get("cmd")
    ]


def _validation_results(evidence: list) -> list:
    seen = set()
    results = []

    for item in _run_results(evidence):
        cmd = item.get("cmd")

        if not any(marker in cmd for marker in _TEST_RUNNER_MARKERS):
            continue

        if cmd in seen:
            continue

        seen.add(cmd)
        results.append(
            {
                "cmd": cmd,
                "returncode": item.get("returncode"),
                "timed_out": bool(item.get("timed_out")),
                "cancelled": bool(item.get("cancelled")),
                "interpretation": validation.interpret_result(item),
            }
        )

    return results


def _diff_summary(evidence: list, output: dict) -> str:
    text = output.get("diff")

    if not isinstance(text, str) or not text:
        diffs = [
            item.get("stdout", "")
            for item in _run_results(evidence)
            if "git diff" in item.get("cmd", "")
        ]
        text = diffs[-1] if diffs else ""

    if len(text) > EVENT_DIFF_LIMIT:
        text = text[:EVENT_DIFF_LIMIT] + "\n... (truncated)"

    return text


def _report_warnings(evidence: list) -> list:
    warnings = []

    for item in evidence:
        if not isinstance(item, dict) or item.get("type") != "diff_scan":
            continue
        report = item.get("report") or {}

        for warning in report.get("warnings", []) or []:
            if warning not in warnings:
                warnings.append(warning)

    return warnings[:_EVENT_WARNINGS_LIMIT]


def build_evidence_report(
    evidence: list,
    output: dict,
    terminal: str,
) -> dict:
    """Deterministic report of exactly what this mission observed.

    No value is invented: unknown fields stay null/empty and are
    derived from real evidence or the worker output.
    """
    output = output or {}
    commands = []

    for item in _run_results(evidence):
        cmd = item.get("cmd")

        if cmd and cmd not in commands:
            commands.append(cmd)

    files_changed = list(output.get("target_files") or [])

    if not files_changed:
        for item in evidence:
            if (
                isinstance(item, dict)
                and item.get("type") == "edit"
                and item.get("file")
            ):
                path = item["file"]

                if path not in files_changed:
                    files_changed.append(path)

    return {
        "terminal": terminal,
        "commands": commands[:EVENT_COMMANDS_LIMIT],
        "files_changed": files_changed,
        "test_results": _validation_results(evidence),
        "diff_summary": _diff_summary(evidence, output),
        "commit_sha": output.get("commit_sha"),
        "working_tree_clean": output.get("working_tree_clean"),
        "warnings": _report_warnings(evidence),
    }


def _terminal_for(success: bool, error: Optional[dict], terminal: Optional[str]) -> str:
    if terminal:
        return terminal
    if success:
        return "PASS"
    err_type = (error or {}).get("type", "")

    if err_type == "Cancelled":
        return "CANCELLED"

    if err_type in ("LLMBlocked", "ToolMissingError"):
        return "BLOCKED"

    return "FAIL"


def _terminal_result(
    success: bool,
    output: dict,
    evidence: list,
    error: Optional[dict],
    retryable: bool,
    terminal: Optional[str] = None,
) -> WorkerResult:
    """Build a WorkerResult with a bounded evidence_report attached."""
    report = build_evidence_report(
        evidence,
        output,
        _terminal_for(success, error, terminal),
    )

    if isinstance(evidence, list):
        evidence.append(
            {
                "type": "evidence_report",
                "evidence_report": report,
                "timestamp": now_iso(),
            }
        )

    if isinstance(output, dict):
        output["evidence_report"] = report

    return WorkerResult(
        success=success,
        output=output,
        evidence=evidence,
        error=error,
        retryable=retryable,
    )


class RepoCodeWorker(Worker):
    name = "repo-code-bud"
    capabilities = {"repo-code"}

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

    def execute(self, goal: str, metadata: Optional[dict] = None) -> WorkerResult:
        metadata = metadata or {}
        evidence = []
        evidence.append({"type": "debug", "message": "WORKER EXECUTE STARTED", "goal": goal})

        repo_path = (
            metadata.get("repo_path")
            or os.environ.get("YODAW_TARGET_REPO")
        )

        if not repo_path:
            return _terminal_result(
                success=False,
                output={},
                evidence=[],
                error={
                    "type": "ConfigError",
                    "message": "repo_path missing and YODAW_TARGET_REPO not set",
                },
                retryable=False,
            )

        # Canonicalize through the shared identity helper and
        # re-check authorization here: a mission admitted before the
        # operator tightened YODAW_REPO_ROOTS must not still reach the
        # filesystem.
        try:
            repo = Path(ensure_authorized(repo_path))
        except RepoNotAllowed as exc:
            return _terminal_result(
                success=False,
                output={},
                evidence=[],
                error={
                    "type": "RepoNotAllowed",
                    "message": str(exc),
                },
                retryable=False,
            )

        if not (repo / ".git").exists():
            return _terminal_result(
                success=False,
                output={"repo": str(repo)},
                evidence=[],
                error={
                    "type": "RepoError",
                    "message": "Target path is not a git repository",
                },
                retryable=False,
            )

        # First, try to get explicit_edits from metadata
        explicit_edits = metadata.get("edits")

        if explicit_edits is None:
            target_file = metadata.get("target_file")
            find_text = metadata.get("find")
            replace_text = metadata.get("replace")

            if (
                target_file
                and find_text is not None
                and replace_text is not None
            ):
                explicit_edits = [
                    {
                        "target_file": target_file,
                        "find": find_text,
                        "replace": replace_text,
                    }
                ]

        # If we still don't have explicit_edits, try to deduce from the goal for simple file modification.
        if explicit_edits is None and goal.startswith("Modify ") and " to say " in goal:
            parts = goal.split(" to say ", 1)
            if len(parts) == 2:
                file_part = parts[0][len("Modify "):].strip()
                new_content = parts[1].strip()
                repo_path = metadata.get("repo_path")
                if repo_path and os.path.exists(repo_path):
                    file_path = os.path.join(repo_path, file_part)
                    if os.path.exists(file_path):
                        try:
                            with open(file_path, 'r') as f:
                                original_content = f.read()
                            explicit_edits = [{
                                "target_file": file_part,
                                "find": original_content,
                                "replace": new_content,
                            }]
                        except Exception:
                            pass

        is_llm_mission = explicit_edits is None

        mission_id = metadata.get("mission_id")
        timeout_seconds = metadata.get("timeout_seconds")

        ctx = CancelContext(
            mission_id,
            metadata.get("_event_store"),
            timeout_seconds=timeout_seconds,
        )
        ctx.emit(
            "worker.started",
            attempt=0,
            goal=goal,
            repo=str(repo) if repo else None,
        )

        try:
            ctx.cancel_check("before_execution")
        except MissionCancelled:
            ctx.emit("mission.cancelled", attempt=0, data={"while": "claimed"})
            raise

        with _mission_runtime(ctx):
            # Everything below runs with the mission's deadline and
            # cancellation bound to every bounded subprocess.
            return self._execute_mission(
                goal,
                metadata,
                ctx,
                repo,
                evidence,
                explicit_edits,
                is_llm_mission,
            )

    def _execute_mission(
        self,
        goal: str,
        metadata: dict,
        ctx: CancelContext,
        repo: Path,
        evidence: list,
        explicit_edits,
        is_llm_mission: bool,
    ) -> WorkerResult:
        worktree_root = Path("workspace").resolve()
        worktree_root.mkdir(exist_ok=True)

        branch_name = metadata.get("branch_name")
        max_retries = int(metadata.get("max_retries", 1))
        retries = 0
        mission_id = metadata.get("mission_id")
        keep_worktree = bool(metadata.get("keep_worktree", False))
        worktree = None

        try:
            # Unique worktree + branch allocation with base-SHA
            # pinning and stale leftovers cleanup (worktree guard).
            # Dirty-source detection lives inside allocate, so a
            # dirty repo is rejected before any worktree is created.
            allocate = worktree_guard.allocate(
                repo,
                worktree_root,
                branch_name=branch_name,
                run=run,
            )
            evidence.append(
                {
                    "type": "worktree_allocate",
                    "worktree": (
                        str(allocate["worktree"])
                        if allocate["worktree"] is not None
                        else None
                    ),
                    "branch": allocate["branch"],
                    "base_sha": allocate["base_sha"],
                    "stale_removed": allocate["stale_removed"],
                    "timestamp": now_iso(),
                }
            )

            if allocate["error"] is not None:
                return _terminal_result(
                    success=False,
                    output={
                        "repo": str(repo),
                        "branch": branch_name,
                    },
                    evidence=evidence,
                    error=allocate["error"],
                    retryable=False,
                )

            worktree = allocate["worktree"]
            branch_name = allocate["branch"]
            base_sha = allocate["base_sha"]

            inspect_result = run(
                ["find", ".", "-maxdepth", "2", "-type", "f"],
                cwd=worktree,
            )
            evidence.append(inspect_result)

            # -------------------------------------------------
            # Mission lifecycle:
            #
            # attempt 0: deterministic edits or initial LLM plan
            # attempt N: LLM repair plan after failed validation
            #
            # Every attempt is validated atomically. Validation
            # failure always restores the attempt baseline before
            # the next plan is requested. Only a final PASS may
            # produce the single mission commit.
            # -------------------------------------------------

            # Detect validation tooling once, before any edit is
            # applied, so a missing tool fails fast with a clear
            # environment error instead of a confusing one.
            try:
                test_commands = detect_test_commands(worktree)
            except ToolMissingError as exc:
                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "goal": goal,
                        "repo": str(repo),
                        "branch": branch_name,
                        "tests_passed": False,
                    },
                    evidence=evidence,
                    error={
                        "type": "ToolMissingError",
                        "message": str(exc),
                    },
                    retryable=False,
                    terminal="BLOCKED",
                )

            if not test_commands:
                # No test runner detected. This is not a vacuous
                # pass: the mission refuses PASS, but containment
                # errors from the plan (path escape, missing text)
                # are surfaced first inside the attempt loop.
                no_tests_detected = True
            else:
                no_tests_detected = False

            lessons = ""

            if is_llm_mission:
                lessons = collect_learning_lessons(
                    goal,
                    evidence,
                    mission_id=mission_id,
                )

            attempt = 0
            previous_plan = None
            repair_error = None

            while True:
                if attempt > 0:
                    ctx.cancel_check(f"between_attempts_{attempt}")

                if attempt == 0:
                    if explicit_edits is not None:
                        edits = explicit_edits
                    else:
                        # Stage 8.4 checkpoint: before the LLM
                        # plan call.
                        ctx.cancel_check("before_llm_plan")

                        try:
                            llm_plan = generate_edit_plan(
                                goal,
                                worktree,
                                lessons=lessons,
                            )

                        except Exception as exc:
                            evidence.extend(provider_attempt_evidence())

                            if isinstance(exc, MissionCancelled):
                                ctx.emit(
                                    "mission.cancelled",
                                    attempt=0,
                                    at="before_llm_plan",
                                )

                                cleanup_worktree(
                                    worktree,
                                    repo,
                                    keep_worktree,
                                    evidence,
                                    failed=True,
                                )

                                return _terminal_result(
                                    success=False,
                                    output={
                                        "goal": goal,
                                        "repo": str(repo),
                                        "worktree": str(worktree),
                                        "branch": branch_name,
                                        "base_sha": base_sha,
                                        "tests_passed": False,
                                        "retries": retries,
                                        "attempts": 1,
                                    },
                                    evidence=evidence,
                                    error={
                                        "type": "Cancelled",
                                        "message": str(exc),
                                        "attempt": 0,
                                    },
                                    retryable=False,
                                    terminal="CANCELLED",
                                )

                            if isinstance(exc, MissionTimeout):
                                cleanup_worktree(
                                    worktree,
                                    repo,
                                    keep_worktree,
                                    evidence,
                                    failed=True,
                                )

                                return _terminal_result(
                                    success=False,
                                    output={
                                        "goal": goal,
                                        "repo": str(repo),
                                        "worktree": str(worktree),
                                        "branch": branch_name,
                                        "base_sha": base_sha,
                                        "tests_passed": False,
                                    },
                                    evidence=evidence,
                                    error={
                                        "type": "Timeout",
                                        "message": str(exc),
                                        "attempt": 0,
                                    },
                                    retryable=False,
                                    terminal="TIMEOUT",
                                )

                            cleanup_worktree(
                                worktree,
                                repo,
                                keep_worktree,
                                evidence,
                                failed=True,
                            )

                            return _terminal_result(
                                success=False,
                                output={
                                    "goal": goal,
                                    "repo": str(repo),
                                    "worktree": str(worktree),
                                    "branch": branch_name,
                                    "base_sha": base_sha,
                                    "tests_passed": False,
                                    "retries": retries,
                                    "attempts": 1,
                                },
                                evidence=evidence,
                                error={
                                    "type": "LLMError",
                                    "message": str(exc),
                                    "attempt": 0,
                                    "retry": retries,
                                },
                                retryable=True,
                            )

                        evidence.append(
                            {
                                "type": "llm_plan",
                                "plan": llm_plan,
                                "timestamp": now_iso(),
                            }
                        )

                        if llm_plan.get("action") == "blocked":
                            cleanup_worktree(
                                worktree,
                                repo,
                                keep_worktree,
                                evidence,
                                failed=True,
                            )

                            return _terminal_result(
                                success=False,
                                output={
                                    "goal": goal,
                                    "repo": str(repo),
                                    "worktree": str(worktree),
                                    "branch": branch_name,
                                    "llm_plan": llm_plan,
                                },
                                evidence=evidence,
                                error={
                                    "type": "LLMBlocked",
                                    "message": llm_plan.get(
                                        "reason",
                                        "Coder Brain blocked task",
                                    ),
                                },
                                retryable=False,
                                terminal="BLOCKED",
                            )

                        edits = normalize_edits(llm_plan)

                        # Stage 8.4: checkpoint after the LLM
                        # call finished but before any work is
                        # prepared or applied.
                        ctx.cancel_check("after_llm_plan")

                        # The initial plan becomes the previous plan
                        # for the first corrective attempt.
                        previous_plan = llm_plan
                else:
                    # -------------------------------------------------
                    # Corrective attempt: feed structured failure
                    # evidence back to the Coder Brain.
                    # -------------------------------------------------
                    try:
                        try:
                            ctx.cancel_check(f"before_repair_{attempt}")
                        except MissionCancelled as exc:
                            repair_error = {
                                "type": "Cancelled",
                                "message": str(exc),
                                "attempt": attempt,
                                "retry": retries,
                            }

                            break
                        except MissionTimeout as exc:
                            repair_error = {
                                "type": "Timeout",
                                "message": str(exc),
                                "attempt": attempt,
                                "retry": retries,
                            }

                            break

                        repair_plan = generate_repair_plan(
                            goal,
                            worktree,
                            previous_plan,
                            build_failure_context(
                                attempt=attempt,
                                test_results=failure_test_results,
                                diff_text=failure_diff_text,
                                touched_files=failure_touched_files,
                            ),
                            lessons=lessons,
                        )

                    except Exception as exc:
                        evidence.extend(provider_attempt_evidence())

                        if isinstance(exc, MissionCancelled):
                            repair_error = {
                                "type": "Cancelled",
                                "message": str(exc),
                                "attempt": attempt,
                                "retry": retries,
                            }

                            break

                        if isinstance(exc, MissionTimeout):
                            raise

                        repair_error = {
                            "type": "LLMError",
                            "message": str(exc),
                            "attempt": attempt,
                            "retry": retries,
                        }

                        break

                    evidence.append(
                        {
                            "type": "repair_plan",
                            "retry": retries,
                            "attempt": attempt,
                            "plan": repair_plan,
                            "timestamp": now_iso(),
                        }
                    )

                    if repair_plan.get("action") == "blocked":
                        cleanup_worktree(
                            worktree,
                            repo,
                            keep_worktree,
                            evidence,
                            failed=True,
                        )

                        return _terminal_result(
                            success=False,
                            output={
                                "goal": goal,
                                "repo": str(repo),
                                "worktree": str(worktree),
                                "branch": branch_name,
                                "repair_plan": repair_plan,
                            },
                            evidence=evidence,
                            error={
                                "type": "LLMBlocked",
                                "message": repair_plan.get(
                                    "reason",
                                    "Coder Brain blocked repair attempt",
                                ),
                            },
                            retryable=False,
                            terminal="BLOCKED",
                        )

                    previous_plan = repair_plan
                    edits = normalize_edits(repair_plan)

                if not isinstance(edits, list) or not edits:
                    return _terminal_result(
                        success=False,
                        output={},
                        evidence=evidence,
                        error={
                            "type": "InvalidEditPlan",
                            "message": "edits must be a non-empty list",
                        },
                        retryable=False,
                    )

                (
                    original_contents,
                    staged_contents,
                    prepared_edits,
                    touched_files,
                    prepare_error,
                ) = prepare_edits(
                    worktree,
                    edits,
                    evidence,
                )

                if prepare_error is not None:
                    cleanup_worktree(
                        worktree,
                        repo,
                        keep_worktree,
                        evidence,
                        failed=True,
                    )

                    output = {"worktree": str(worktree)}
                    output.update(prepare_error["output"])

                    return _terminal_result(
                        success=False,
                        output=output,
                        evidence=evidence,
                        error=prepare_error["error"],
                        retryable=False,
                    )

                ctx.cancel_check(f"before_apply_attempt_{attempt}")

                if no_tests_detected:
                    # The plan is safe (containment validated), but
                    # with no test runner an empty validation plan
                    # must never silently pass a mission: refuse the
                    # pass and commit nothing.
                    cleanup_worktree(
                        worktree,
                        repo,
                        keep_worktree,
                        evidence,
                        failed=True,
                    )

                    return _terminal_result(
                        success=False,
                        output={
                            "goal": goal,
                            "repo": str(repo),
                            "worktree": str(worktree),
                            "branch": branch_name,
                            "base_sha": base_sha,
                            "tests_passed": False,
                            "tests_detected": 0,
                        },
                        evidence=evidence,
                        error={
                            "type": "NoTestsDetected",
                            "message": (
                                "no test runner detected; refusing "
                                "a zero-test pass"
                            ),
                        },
                        retryable=False,
                    )

                apply_edits(
                    worktree,
                    prepared_edits,
                    staged_contents,
                )

                ctx.emit(
                    "edit.applied",
                    attempt=attempt,
                    files=touched_files,
                )

                for item in prepared_edits:
                    evidence.append(
                        {
                            "type": "edit",
                            "edit_index": item["edit_index"],
                            "file": item["target_file"],
                            "find": item.get("find"),
                            "replace": item.get("replace"),
                            "action": item.get("action", "edit"),
                            "timestamp": now_iso(),
                        }
                    )

                ctx.cancel_check(f"before_validation_attempt_{attempt}")
                ctx.emit("validation.started", attempt=attempt)

                tests_passed, test_results = run_validation(
                    worktree,
                    evidence,
                    test_commands,
                )

                if tests_passed:
                    break

                # ---------------------------------------------
                # Validation failed: capture evidence, restore
                # the attempt baseline, then decide whether a
                # corrective attempt is allowed.
                # ---------------------------------------------
                diff_result = run(
                    ["git", "diff", "--", *touched_files],
                    cwd=worktree,
                )
                evidence.append(diff_result)

                evidence.append(
                    {
                        "type": "validation_failure",
                        "attempt": attempt,
                        "tests": test_results,
                        "diff": diff_result["stdout"],
                        "files": touched_files,
                        "timestamp": now_iso(),
                    }
                )

                failure_test_results = test_results
                failure_diff_text = diff_result["stdout"]
                failure_touched_files = touched_files

                restore_originals(
                    worktree,
                    original_contents,
                    prepared_edits,
                    evidence,
                    retries,
                )

                clean_after_restore = worktree_is_clean(worktree)

                evidence.append(
                    {
                        "type": "restore_check",
                        "attempt": attempt,
                        "worktree_clean": clean_after_restore,
                        "timestamp": now_iso(),
                    }
                )

                if not clean_after_restore:
                    cleanup_worktree(
                        worktree,
                        repo,
                        keep_worktree,
                        evidence,
                        failed=True,
                    )

                    return _terminal_result(
                        success=False,
                        output={
                            "goal": goal,
                            "repo": str(repo),
                            "worktree": str(worktree),
                            "branch": branch_name,
                            "base_sha": base_sha,
                            "tests_passed": False,
                            "retries": retries,
                            "attempts": attempt + 1,
                            "diff": diff_result["stdout"],
                        },
                        evidence=evidence,
                        error={
                            "type": "RestoreError",
                            "message": (
                                "Worktree not clean after restoring "
                                "failed edits"
                            ),
                        },
                        retryable=False,
                    )

                if not is_llm_mission:
                    # Deterministic metadata edits carry no
                    # autonomous repair path in this stage.
                    break

                if retries >= max_retries:
                    repair_error = {
                        "type": "RetryExhausted",
                        "message": (
                            "Validation failed and retry budget "
                            "is exhausted"
                        ),
                        "attempt": attempt,
                        "retry": retries,
                    }

                    break

                retries += 1
                attempt += 1

            if repair_error is not None:
                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "goal": goal,
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                        "base_sha": base_sha,
                        "tests_passed": False,
                        "retries": retries,
                        "attempts": attempt + 1,
                    },
                    evidence=evidence,
                    error=repair_error,
                    retryable=True,
                )

            if not tests_passed:
                # Final attempt failed validation (e.g. deterministic
                # edits with no repair path); nothing was committed.
                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "goal": goal,
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                        "base_sha": base_sha,
                        "tests_passed": False,
                        "retries": retries,
                        "attempts": attempt + 1,
                        "diff": failure_diff_text,
                    },
                    evidence=evidence,
                    error={
                        "type": "ValidationFailed",
                        "message": "Tests failed; change was not committed",
                    },
                    retryable=True,
                )

            status_before_commit = run(
                ["git", "status", "--short"],
                cwd=worktree,
            )
            evidence.append(status_before_commit)

            if not status_before_commit["stdout"].strip():
                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "goal": goal,
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                    },
                    evidence=evidence,
                    error={
                        "type": "NoChange",
                        "message": "Edit produced no repository change",
                    },
                    retryable=False,
                )

            diff_result = run(
                ["git", "diff", "--", *touched_files],
                cwd=worktree,
            )
            evidence.append(diff_result)

            add_result = run(
                ["git", "add", "--", *touched_files],
                cwd=worktree,
            )
            evidence.append(add_result)

            # Cancellation before commit must leave the source
            # repository untouched: no commit, no merge, no push.
            ctx.cancel_check("before_commit")

            # Commit fence: the source repository must not have
            # moved off the pinned base SHA while this mission ran.
            try:
                worktree_guard.verify_commit_fence(
                    repo,
                    worktree,
                    base_sha=base_sha,
                    run=run,
                    expect_clean=False,
                )
            except worktree_guard.FenceViolation as exc:
                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                        "base_sha": base_sha,
                    },
                    evidence=evidence,
                    error={
                        "type": "FenceViolation",
                        "message": str(exc),
                    },
                    retryable=False,
                )

            # Secret / credential guard: nothing staged may contain
            # detected credentials; warnings (artifacts) are recorded
            # but advisory.
            staged_diff = run(
                ["git", "diff", "--cached"],
                cwd=worktree,
            )
            evidence.append(staged_diff)

            staged_scan = diff_guard.scan(staged_diff.get("stdout", ""))
            path_scan = diff_guard.scan_paths(touched_files)
            merged_scan = {
                "blocking": staged_scan["blocking"] + [
                    finding
                    for finding in path_scan["blocking"]
                    if finding not in staged_scan["blocking"]
                ],
                "warnings": staged_scan["warnings"] + [
                    finding
                    for finding in path_scan["warnings"]
                    if finding not in staged_scan["warnings"]
                ],
                "ok": not (
                    staged_scan["blocking"] or path_scan["blocking"]
                ),
            }
            evidence.append(
                {
                    "type": "diff_scan",
                    "report": merged_scan,
                    "timestamp": now_iso(),
                }
            )

            if merged_scan["blocking"]:
                # The worktree (and its full evidence) is preserved
                # for inspection; nothing is committed.
                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                        "base_sha": base_sha,
                        "secret_blocked": True,
                        "diff_scan": merged_scan,
                    },
                    evidence=evidence,
                    error={
                        "type": "SecretBlocked",
                        "message": (
                            "staged changes contain detected "
                            "credentials; nothing committed"
                        ),
                        "blocking": merged_scan["blocking"],
                    },
                    retryable=False,
                )

            commit_message = metadata.get(
                "commit_message",
                f"yodaw: {goal[:72]}",
            )

            commit_result = run(
                ["git", "commit", "-m", commit_message],
                cwd=worktree,
            )
            evidence.append(commit_result)

            if commit_result["returncode"] != 0:
                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                    },
                    evidence=evidence,
                    error={
                        "type": "CommitError",
                        "message": commit_result["stderr"] or commit_result["stdout"],
                    },
                    retryable=False,
                )

            sha_result = run(
                ["git", "rev-parse", "HEAD"],
                cwd=worktree,
            )
            evidence.append(sha_result)

            final_status = run(
                ["git", "status", "--short"],
                cwd=worktree,
            )
            evidence.append(final_status)

            # Successful mission: remove the isolated worktree so
            # workspace/ does not accumulate directories. This runs
            # only after the commit is fully recorded.
            cleanup_worktree(
                worktree,
                repo,
                keep_worktree,
                evidence,
                failed=False,
            )

            return _terminal_result(
                success=True,
                output={
                    "goal": goal,
                    "repo": str(repo),
                    "worktree": str(worktree),
                    "branch": branch_name,
                    "base_sha": base_sha,
                    "commit_sha": sha_result["stdout"].strip(),
                    # Legacy field retained for compatibility.
                    "target_file": (
                        touched_files[0]
                        if len(touched_files) == 1
                        else None
                    ),
                    "target_files": touched_files,
                    "edit_count": len(prepared_edits),
                    "tests_detected": len(test_commands),
                    "tests_passed": True,
                    "retries": retries,
                    "attempts": attempt + 1,
                    "diff": diff_result["stdout"],
                    "working_tree_clean": final_status["stdout"].strip() == "",
                },
                evidence=evidence,
                error=None,
                retryable=False,
            )

        except MissionTimeout as exc:
            ctx.emit(
                "mission.timed_out",
                attempt=0,
                at="worker_execution",
            )

            cleanup_worktree(
                worktree,
                repo,
                keep_worktree,
                evidence,
                failed=True,
            )

            return _terminal_result(
                success=False,
                output={
                    "goal": goal,
                    "repo": str(repo),
                    "worktree": str(worktree),
                    "branch": branch_name,
                },
                evidence=evidence,
                error={
                    "type": "Timeout",
                    "message": str(exc),
                    "at": "worker_execution",
                },
                retryable=False,
                terminal="TIMEOUT",
            )

        except Exception as exc:
            evidence.extend(provider_attempt_evidence())

            if isinstance(exc, MissionCancelled):
                ctx.emit(
                    "mission.cancelled",
                    attempt=0,
                    at="worker_execution",
                )

                cleanup_worktree(
                    worktree,
                    repo,
                    keep_worktree,
                    evidence,
                    failed=True,
                )

                return _terminal_result(
                    success=False,
                    output={
                        "goal": goal,
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                    },
                    evidence=evidence,
                    error={
                        "type": "Cancelled",
                        "message": str(exc),
                    },
                    retryable=False,
                    terminal="CANCELLED",
                )

            # Unhandled worker failure: still record the worktree
            # lifecycle so cleanup is never silent.
            cleanup_worktree(
                worktree,
                repo,
                keep_worktree,
                evidence,
                failed=True,
            )

            return _terminal_result(
                success=False,
                output={
                    "goal": goal,
                    "repo": str(repo),
                    "worktree": str(worktree),
                    "branch": branch_name,
                },
                evidence=evidence,
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                retryable=False,
            )