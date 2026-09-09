import os
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from app.workers.base import Worker, WorkerResult
from app.llm.coder import generate_edit_plan, generate_repair_plan


def now_iso():
    return datetime.now(timezone.utc).isoformat()


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
        commands.append(
            [
                "python",
                "-B",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
            ]
        )

    if (worktree / "package.json").exists():
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
            original_contents[target_file] = target.read_text()

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
        target.write_text(staged_contents[target_file])


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
        edited_path.write_text(original_text)

    evidence.append(
        {
            "type": "recovery",
            "action": "revert_failed_edits",
            "files": list(original_contents.keys()),
            "retry": retry,
            "timestamp": now_iso(),
        }
    )


def run_validation(worktree: Path, evidence: list):
    """
    Run every detected test command once and return
    (passed, results).
    """
    test_commands = detect_test_commands(worktree)
    results = []

    for cmd in test_commands:
        result = run(cmd, cwd=worktree)
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

            attempt = 0
            previous_plan = None
            repair_error = None

            while True:
                if attempt == 0:
                    if explicit_edits is not None:
                        edits = explicit_edits
                    else:
                        llm_plan = generate_edit_plan(
                            goal,
                            worktree,
                        )

                        evidence.append(
                            {
                                "type": "llm_plan",
                                "plan": llm_plan,
                                "timestamp": now_iso(),
                            }
                        )

                        if llm_plan.get("action") == "blocked":
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

                        # The initial plan becomes the previous plan
                        # for the first corrective attempt.
                        previous_plan = llm_plan
                else:
                    # -------------------------------------------------
                    # Corrective attempt: feed structured failure
                    # evidence back to the Coder Brain.
                    # -------------------------------------------------
                    try:
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
                        )

                    except Exception as exc:
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
                    return WorkerResult(
                        success=False,
                        output=prepare_error["output"],
                        evidence=evidence,
                        error=prepare_error["error"],
                        retryable=False,
                    )

                apply_edits(
                    worktree,
                    staged_contents,
                    touched_files,
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

                tests_passed, test_results = run_validation(
                    worktree,
                    evidence,
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
                    "tests_detected": len(detect_test_commands(worktree)),
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
