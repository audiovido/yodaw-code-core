import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from app.workers.base import Worker, WorkerResult
from app.llm.coder import generate_edit_plan, generate_repair_plan
from app.learning.retrieval import (
    format_lessons,
    retrieve_relevant_learnings,
)
from app.llm.provider import pop_attempt_log


class MissionCancelled(Exception):
    """Raised at cancellation checkpoints inside the worker."""


class CancelContext:
    """
    Stage 8.4/8.8: cancellation and event context for one
    executing mission.

    cancel_check() raises MissionCancelled when cancellation was
    requested; the worker calls it at every checkpoint (before
    LLM calls, before applying edits, before validation, between
    repair attempts, before commit) so a cancelled mission never
    commits and never keeps running.

    emit() persists structured mission events for observability;
    it must never break mission execution.
    """

    def __init__(self, mission_id: str | None, store=None):
        self.mission_id = mission_id
        self.store = store
        self.cancel_requested = False

    def bind(self, store, mission_id: str | None):
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


class ToolMissingError(RuntimeError):
    """A validation tool is not available in the environment."""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def which(tool: str):
    return shutil.which(tool)


def read_text_preserve(path: Path) -> str:
    """Read file text without newline translation so CRLF files
    keep their exact line endings in memory."""
    with path.open("r", newline="") as handle:
        return handle.read()


def write_text_preserve(path: Path, text: str) -> None:
    """Write file text without newline translation so untouched
    regions keep their original line endings byte-for-byte."""
    with path.open("w", newline="") as handle:
        handle.write(text)


def run(cmd, cwd=None, timeout=300):
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    return {
        "cmd": " ".join(cmd),
        "cwd": str(cwd) if cwd else None,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
        "timestamp": now_iso(),
    }


def find_relaxed_unique_match(original: str, needle: str):
    """
    Find a unique multiline match while ignoring leading/trailing
    whitespace on each line. Returns the exact source slice so the
    replacement still applies to real repository text.
    """
    needle_lines = needle.strip().splitlines()
    if not needle_lines:
        return None

    original_lines = original.splitlines(keepends=True)
    normalized_needle = [line.strip() for line in needle_lines]

    matches = []

    for start in range(len(original_lines)):
        end = start + len(normalized_needle)

        if end > len(original_lines):
            break

        candidate = original_lines[start:end]

        if [line.strip() for line in candidate] == normalized_needle:
            matches.append("".join(candidate))

    if len(matches) == 1:
        return matches[0]

    return None


def detect_test_commands(worktree: Path):
    commands = []

    if (
        (worktree / "pytest.ini").exists()
        or (worktree / "tests").exists()
        or (worktree / "pyproject.toml").exists()
    ):
        # Run under the interpreter that is executing YODAW itself
        # instead of relying on a PATH lookup; this avoids confusing
        # failures when 'python' is not on PATH while never
        # hardcoding a user-specific environment path.
        commands.append(
            [
                sys.executable or "python",
                "-B",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
            ]
        )

    if (worktree / "package.json").exists():
        if not which("npm"):
            raise ToolMissingError(
                "npm executable not found on PATH; "
                "cannot run JavaScript validation"
            )

        commands.append(["npm", "test", "--", "--runInBand"])

    return commands


def normalize_edits(plan: dict) -> list | None:
    """
    Accept either the structured multi-edit plan or the legacy
    Stage 6 single-edit shape and return an edits list, or None
    when the plan carries no recognizable edit payload.
    """
    if not isinstance(plan, dict):
        return None

    edits = plan.get("edits")

    if isinstance(edits, list):
        return edits

    legacy_target = plan.get("target_file")
    legacy_find = plan.get("find")
    legacy_replace = plan.get("replace")

    if legacy_target and legacy_find is not None and legacy_replace is not None:
        return [
            {
                "target_file": legacy_target,
                "find": legacy_find,
                "replace": legacy_replace,
            }
        ]

    return None


def prepare_edits(
    worktree: Path,
    edits: list,
    evidence: list,
):
    """
    Validate every edit and stage all resulting file contents in
    memory. Nothing is written until every edit is known-valid.

    Returns (original_contents, staged_contents, prepared_edits,
    touched_files, error) where error is None on success.
    """
    staged_contents = {}
    original_contents = {}
    prepared_edits = []
    touched_files = []

    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            return None, None, None, None, {
                "output": {"edit_index": index},
                "error": {
                    "type": "InvalidEditPlan",
                    "message": f"Edit {index} is not an object",
                },
            }

        required = ("target_file", "find", "replace")
        missing = [key for key in required if key not in edit]

        if missing:
            return None, None, None, None, {
                "output": {"edit_index": index},
                "error": {
                    "type": "InvalidEditPlan",
                    "message": f"Edit {index} missing fields: {missing}",
                },
            }

        target_file = edit["target_file"]
        find_text = edit["find"]
        replace_text = edit["replace"]

        target = (worktree / target_file).resolve()

        if worktree.resolve() not in target.parents:
            return None, None, None, None, {
                "output": {
                    "edit_index": index,
                    "target_file": target_file,
                },
                "error": {
                    "type": "PathEscapeError",
                    "message": f"{target_file} escapes isolated worktree",
                },
            }

        if not target.exists():
            return None, None, None, None, {
                "output": {
                    "edit_index": index,
                    "target_file": str(target),
                },
                "error": {
                    "type": "TargetNotFound",
                    "message": f"{target_file} does not exist",
                },
            }

        if not target.is_file():
            return None, None, None, None, {
                "output": {
                    "edit_index": index,
                    "target_file": str(target),
                },
                "error": {
                    "type": "TargetNotFile",
                    "message": f"{target_file} is not a file",
                },
            }

        if target_file not in original_contents:
            original_contents[target_file] = read_text_preserve(target)

        current = staged_contents.get(
            target_file,
            original_contents[target_file],
        )

        actual_find_text = find_text

        if find_text not in current:
            actual_find_text = find_relaxed_unique_match(
                current,
                find_text,
            )

            if actual_find_text is not None:
                evidence.append(
                    {
                        "type": "relaxed_match",
                        "file": target_file,
                        "requested_find": find_text,
                        "actual_find": actual_find_text,
                        "edit_index": index,
                        "timestamp": now_iso(),
                    }
                )

        if actual_find_text is None or actual_find_text not in current:
            return None, None, None, None, {
                "output": {
                    "edit_index": index,
                    "target_file": target_file,
                },
                "error": {
                    "type": "FindTextMissing",
                    "message": (
                        "Requested source text was not found "
                        "as an exact or unique relaxed match"
                    ),
                },
            }

        modified = current.replace(
            actual_find_text,
            replace_text,
            1,
        )

        staged_contents[target_file] = modified

        if target_file not in touched_files:
            touched_files.append(target_file)

        prepared_edits.append(
            {
                "edit_index": index,
                "target_file": target_file,
                "find": find_text,
                "actual_find": actual_find_text,
                "replace": replace_text,
            }
        )

    return (
        original_contents,
        staged_contents,
        prepared_edits,
        touched_files,
        None,
    )


def apply_edits(worktree: Path, staged_contents: dict, touched_files: list):
    """
    Atomic apply: every edit is already known-valid, so every
    touched file can now be written.
    """
    for target_file in touched_files:
        target = (worktree / target_file).resolve()
        write_text_preserve(target, staged_contents[target_file])


def restore_originals(
    worktree: Path,
    original_contents: dict,
    evidence: list,
    retry: int,
):
    """
    Revert ALL edits by restoring every touched file's original
    text. Must always run after a failed validation so no
    half-validated change is ever left behind.
    """
    for edited_file, original_text in original_contents.items():
        edited_path = (worktree / edited_file).resolve()
        write_text_preserve(edited_path, original_text)

    evidence.append(
        {
            "type": "recovery",
            "action": "revert_failed_edits",
            "files": list(original_contents.keys()),
            "retry": retry,
            "timestamp": now_iso(),
        }
    )


def run_validation(
    worktree: Path,
    evidence: list,
    test_commands: list,
):
    """
    Run every detected test command once and return
    (passed, results).
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
        item["returncode"] == 0 for item in results
    ) if results else True

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
    mission_id: str | None = None,
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
    Safe worktree lifecycle cleanup.

    Policy:
    - keep_worktree=True keeps the worktree for any outcome.
    - failed missions keep their worktree explicitly for
      debugging/evidence (recorded, never silent).
    - successful missions remove the worktree and prune.

    Cleanup failure must never hide the original mission result;
    it is recorded as worktree_cleanup_error evidence instead.
    """
    if keep_worktree:
        action = "kept_by_request"
    elif failed:
        action = "kept_failed_for_debugging"
    else:
        action = "removed"

    evidence.append(
        {
            "type": "worktree_cleanup",
            "action": action,
            "worktree": str(worktree),
            "timestamp": now_iso(),
        }
    )

    if action != "removed":
        return

    try:
        remove_result = run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=repo,
        )
        evidence.append(remove_result)

        if remove_result["returncode"] != 0:
            evidence.append(
                {
                    "type": "worktree_cleanup_error",
                    "worktree": str(worktree),
                    "error": (
                        "git worktree remove failed; "
                        "used filesystem fallback"
                    ),
                    "timestamp": now_iso(),
                }
            )

            shutil.rmtree(worktree, ignore_errors=True)

        prune_result = run(
            ["git", "worktree", "prune"],
            cwd=repo,
        )
        evidence.append(prune_result)

    except Exception as exc:
        evidence.append(
            {
                "type": "worktree_cleanup_error",
                "worktree": str(worktree),
                "error": str(exc),
                "timestamp": now_iso(),
            }
        )

        try:
            shutil.rmtree(worktree, ignore_errors=True)
            run(["git", "worktree", "prune"], cwd=repo)
        except Exception:
            # Cleanup is best-effort; the mission result stands.
            pass


def worktree_is_clean(worktree: Path) -> bool:
    status = run(
        ["git", "status", "--short"],
        cwd=worktree,
    )

    return status["returncode"] == 0 and not status["stdout"].strip()


class RepoCodeWorker(Worker):
    name = "repo-code-bud"
    capabilities = {"repo-code"}

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

    def execute(self, goal: str, metadata: dict | None = None) -> WorkerResult:
        metadata = metadata or {}
        evidence = []

        repo_path = (
            metadata.get("repo_path")
            or os.environ.get("YODAW_TARGET_REPO")
        )

        if not repo_path:
            return WorkerResult(
                success=False,
                output={},
                evidence=[],
                error={
                    "type": "ConfigError",
                    "message": "repo_path missing and YODAW_TARGET_REPO not set",
                },
                retryable=False,
            )

        repo = Path(repo_path).expanduser().resolve()

        if not (repo / ".git").exists():
            return WorkerResult(
                success=False,
                output={"repo": str(repo)},
                evidence=[],
                error={
                    "type": "RepoError",
                    "message": "Target path is not a git repository",
                },
                retryable=False,
            )

        worktree_root = Path("workspace")
        worktree_root.mkdir(exist_ok=True)

        branch_name = (
            metadata.get("branch_name")
            or f"yodaw/task-{int(datetime.now().timestamp())}"
        )

        worktree = Path(
            tempfile.mkdtemp(prefix="repo_", dir=worktree_root)
        )
        worktree.rmdir()

        max_retries = int(metadata.get("max_retries", 1))
        retries = 0
        mission_id = metadata.get("mission_id")
        keep_worktree = bool(metadata.get("keep_worktree", False))

        ctx = CancelContext(
            metadata.get("mission_id"),
            metadata.get("_event_store"),
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

        try:
            source_status = run(
                ["git", "status", "--short"],
                cwd=repo,
            )
            evidence.append(source_status)

            if source_status["stdout"].strip():
                return WorkerResult(
                    success=False,
                    output={"repo": str(repo)},
                    evidence=evidence,
                    error={
                        "type": "DirtyRepo",
                        "message": "Source repository has uncommitted changes",
                    },
                    retryable=False,
                )

            base_sha = run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
            )
            evidence.append(base_sha)

            wt_result = run(
                [
                    "git",
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree),
                    "HEAD",
                ],
                cwd=repo,
            )
            evidence.append(wt_result)

            if wt_result["returncode"] != 0:
                return WorkerResult(
                    success=False,
                    output={
                        "repo": str(repo),
                        "branch": branch_name,
                    },
                    evidence=evidence,
                    error={
                        "type": "WorktreeError",
                        "message": wt_result["stderr"] or wt_result["stdout"],
                    },
                    retryable=False,
                )

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

            is_llm_mission = explicit_edits is None

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

                return WorkerResult(
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
                )

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

                                return WorkerResult(
                                    success=False,
                                    output={
                                        "goal": goal,
                                        "repo": str(repo),
                                        "worktree": str(worktree),
                                        "branch": branch_name,
                                        "base_sha": base_sha["stdout"].strip(),
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
                                )

                            cleanup_worktree(
                                worktree,
                                repo,
                                keep_worktree,
                                evidence,
                                failed=True,
                            )

                            return WorkerResult(
                                success=False,
                                output={
                                    "goal": goal,
                                    "repo": str(repo),
                                    "worktree": str(worktree),
                                    "branch": branch_name,
                                    "base_sha": base_sha["stdout"].strip(),
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

                            return WorkerResult(
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

                        return WorkerResult(
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
                        )

                    previous_plan = repair_plan
                    edits = normalize_edits(repair_plan)

                if not isinstance(edits, list) or not edits:
                    return WorkerResult(
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

                    return WorkerResult(
                        success=False,
                        output=output,
                        evidence=evidence,
                        error=prepare_error["error"],
                        retryable=False,
                    )

                ctx.cancel_check(f"before_apply_attempt_{attempt}")

                apply_edits(
                    worktree,
                    staged_contents,
                    touched_files,
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
                            "find": item["find"],
                            "replace": item["replace"],
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

                    return WorkerResult(
                        success=False,
                        output={
                            "goal": goal,
                            "repo": str(repo),
                            "worktree": str(worktree),
                            "branch": branch_name,
                            "base_sha": base_sha["stdout"].strip(),
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

                return WorkerResult(
                    success=False,
                    output={
                        "goal": goal,
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                        "base_sha": base_sha["stdout"].strip(),
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

                return WorkerResult(
                    success=False,
                    output={
                        "goal": goal,
                        "repo": str(repo),
                        "worktree": str(worktree),
                        "branch": branch_name,
                        "base_sha": base_sha["stdout"].strip(),
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

                return WorkerResult(
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

                return WorkerResult(
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

            return WorkerResult(
                success=True,
                output={
                    "goal": goal,
                    "repo": str(repo),
                    "worktree": str(worktree),
                    "branch": branch_name,
                    "base_sha": base_sha["stdout"].strip(),
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
                retryable=False,
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

                return WorkerResult(
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

            return WorkerResult(
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
