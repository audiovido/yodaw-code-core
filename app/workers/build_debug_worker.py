"""Build, debug, and repair worker for universal execution loop."""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.workers.base import Worker, WorkerResult
from app.workers.repo_code_worker import (
    CancelContext,
    MissionCancelled,
    RepoNotAllowed,
    ensure_authorized,
    run,
    which,
    shutil,
    now_iso,
    ToolMissingError,
    normalize_edits,
    prepare_edits,
    apply_edits,
    restore_originals,
    run_validation,
    build_failure_context,
    collect_learning_lessons,
    cleanup_worktree,
    worktree_is_clean,
    provider_attempt_evidence,
    resolve_python_executable,
)
from app.llm.coder import generate_edit_plan, generate_repair_plan
from app.reuse.analyzer import detect_project_type


class BuildDebugWorker(Worker):
    """Worker that executes the universal build→test→debug→repair loop."""

    name = "build-debug-bud"
    capabilities = {"build-debug"}

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

    def execute(self, goal: str, metadata: Optional[dict] = None) -> WorkerResult:
        """Execute the build-debug-repair loop."""
        print(f"BUILD_DEBUG_WORKER: execute called with goal={goal!r}, metadata={metadata!r}", flush=True)
        metadata = metadata or {}
        evidence = []

        repo_path = (
            metadata.get("repo_path")
            or os.environ.get("YODAW_TARGET_REPO")
        )

        if not repo_path:
            print("BUILD_DEBUG_WORKER: no repo_path", flush=True)
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

        # Canonicalize through the shared identity helper and
        # re-check authorization here: a mission admitted before the
        # operator tightened YODAW_REPO_ROOTS must not still reach the
        # filesystem.
        try:
            repo = Path(ensure_authorized(repo_path))
        except RepoNotAllowed as exc:
            return WorkerResult(
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
            print(f"BUILD_DEBUG_WORKER: git check failed for {repo}", flush=True)
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

        print(f"BUILD_DEBUG_WORKER: past git check", flush=True)

        # Extract loop parameters
        max_attempts = int(metadata.get("max_attempts", 3))
        max_time_seconds = int(metadata.get("max_time_seconds", 300))
        keep_worktree = bool(metadata.get("keep_worktree", False))
        mission_id = metadata.get("mission_id")
        _event_store = metadata.get("_event_store")

        # Initialize cancellation context
        ctx = CancelContext(mission_id, _event_store)
        ctx.emit("worker.started", attempt=0, goal=goal, repo=str(repo) if repo else None)

        try:
            ctx.cancel_check("before_execution")
        except MissionCancelled:
            ctx.emit("mission.cancelled", attempt=0, data={"while": "claimed"})
            raise

        # Start timing
        start_time = time.time()

        # Create worktree
        worktree_root = Path("workspace").resolve()
        worktree_root.mkdir(exist_ok=True)

        branch_name = (
            metadata.get("branch_name")
            or f"yodaw/build-debug-{int(datetime.now().timestamp())}"
        )

        worktree = worktree_root / f"repo_{int(datetime.now().timestamp())}"
        # Ensure the worktree directory does not exist (remove if it does)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)

        try:
            # Check source repo status
            source_status = run(["git", "status", "--short"], cwd=repo)
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

            base_sha = run(["git", "rev-parse", "HEAD"], cwd=repo)
            evidence.append(base_sha)

            # Create worktree
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

            # Main build-debug-repair loop
            attempt = 0
            retries = 0
            lessons = ""
            repair_error = None

            while attempt < max_attempts and (time.time() - start_time) < max_time_seconds:
                # Check for timeouts between attempts
                if attempt > 0:
                    ctx.cancel_check(f"between_attempts_{attempt}")

                # Check timeout
                elapsed = time.time() - start_time
                if elapsed >= max_time_seconds:
                    break

                print(f"BUILD_DEBUG_WORKER: attempt {attempt}", flush=True)

                # Detect project type
                project_type = detect_project_type(worktree)

                # Attempt 0: Try to get explicit edits or generate initial plan
                if attempt == 0:
                    explicit_edits = metadata.get("edits")
                    target_file = metadata.get("target_file")
                    find_text = metadata.get("find")
                    replace_text = metadata.get("replace")

                    if (
                        explicit_edits is None
                        and target_file
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

                    # If we still don't have explicit_edits, try to deduce from the goal
                    if explicit_edits is None and goal.startswith("Modify ") and " to say " in goal:
                        parts = goal.split(" to say ", 1)
                        if len(parts) == 2:
                            file_part = parts[0][len("Modify "):].strip()
                            new_content = parts[1].strip()
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

                    if is_llm_mission:
                        # Collect learning lessons for LLM missions
                        lessons = collect_learning_lessons(
                            goal,
                            evidence,
                            mission_id=mission_id,
                        )

                    # Check cancellation before LLM plan
                    ctx.cancel_check("before_llm_plan")

                    try:
                        if explicit_edits is not None:
                            llm_plan = {"edits": explicit_edits, "action": "edit"}
                        else:
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

                    # Check cancellation after LLM plan
                    ctx.cancel_check("after_llm_plan")

                    # The initial plan becomes the previous plan for the first corrective attempt.
                    previous_plan = llm_plan
                else:
                    # Corrective attempt: feed structured failure evidence back to the Coder Brain.
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

                # Detect test commands based on project type
                test_commands = self._detect_test_commands(worktree, project_type)

                tests_passed, test_results = run_validation(
                    worktree,
                    evidence,
                    test_commands,
                )

                if tests_passed:
                    break

                # Validation failed: capture evidence, restore the attempt baseline
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
                    # Deterministic metadata edits carry no autonomous repair path
                    break

                if retries >= max_attempts - 1:  # We already used one attempt
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

            # Handle loop termination conditions
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
                # Final attempt failed validation
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

            # Success path - commit changes
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

            # Cancellation before commit must leave the source repository untouched
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

            # Successful mission: remove the isolated worktree
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

            # Unhandled worker failure: still record the worktree lifecycle
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

    def _detect_test_commands(self, worktree: Path, project_type: dict) -> list:
        """Detect test commands based on project type."""
        commands = []

        # Python
        if project_type["python"]:
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
                        resolve_python_executable(),
                        "-B",
                        "-m",
                        "pytest",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                    ]
                )

        # Node.js
        if project_type["node"]:
            if (worktree / "package.json").exists():
                if not which("npm"):
                    raise ToolMissingError(
                        "npm executable not found on PATH; "
                        "cannot run JavaScript validation"
                    )

                commands.append(["npm", "test", "--", "--runInBand"])

        # Rust
        if project_type["rust"]:
            if not which("cargo"):
                raise ToolMissingError(
                    "cargo executable not found on PATH; "
                    "cannot run Rust validation"
                )
            commands.append(["cargo", "test"])

        # Go
        if project_type["go"]:
            if not which("go"):
                raise ToolMissingError(
                    "go executable not found on PATH; "
                    "cannot run Go validation"
                )
            commands.append(["go", "test", "./..."])

        # Java
        if project_type["java"]:
            # Prefer Maven wrapper, then Gradle wrapper, then plain mvn/gradle
            if (worktree / "pom.xml").exists():
                if which("mvn"):
                    commands.append(["mvn", "test"])
                elif (worktree / "mvnw").exists():
                    commands.append(["./mvnw", "test"])
                else:
                    raise ToolMissingError(
                        "mvn executable not found on PATH and no mvnw wrapper; "
                        "cannot run Java validation"
                    )
            elif (worktree / "build.gradle").exists() or (worktree / "build.gradle.kts").exists():
                if which("gradle"):
                    commands.append(["gradle", "test"])
                elif (worktree / "gradlew").exists():
                    commands.append(["./gradlew", "test"])
                else:
                    raise ToolMissingError(
                        "gradle executable not found on PATH and no gradlew wrapper; "
                        "cannot run Java validation"
                    )

        # Swift
        if project_type["swift"]:
            # We assume xcodebuild is available on macOS; we'll check for it
            if not which("xcodebuild"):
                raise ToolMissingError(
                    "xcodebuild executable not found on PATH; "
                    "cannot run Swift validation"
                )
            # We run tests for all schemes; note: this might be slow and require a simulator/device
            commands.append(["xcodebuild", "test"])

        # C/C++
        if project_type["c_cpp"]:
            # Try to detect if there's a test target in CMake or Makefile
            if (worktree / "CMakeLists.txt").exists():
                # We'll try to run ctest if available
                if which("ctest"):
                    commands.append(["ctest"])
                else:
                    # Fallback to make test if there's a Makefile
                    if (worktree / "Makefile").exists():
                        if which("make"):
                            commands.append(["make", "test"])
                        else:
                            raise ToolMissingError(
                                "make executable not found on PATH; "
                                "cannot run C/C++ validation"
                            )
                    else:
                        raise ToolMissingError(
                            "ctest executable not found on PATH and no Makefile; "
                            "cannot run C/C++ validation"
                        )
            elif (worktree / "Makefile").exists():
                if which("make"):
                    commands.append(["make", "test"])
                else:
                    raise ToolMissingError(
                        "make executable not found on PATH; "
                        "cannot run C/C++ validation"
                    )

        # .NET
        if project_type["dotnet"]:
            if not which("dotnet"):
                raise ToolMissingError(
                    "dotnet executable not found on PATH; "
                    "cannot run .NET validation"
                )
            # We look for a solution file or project file and run dotnet test in the root
            commands.append(["dotnet", "test"])

        # Android
        if project_type["android"]:
            # We treat Android as a Gradle project (since it uses Gradle)
            if (worktree / "build.gradle").exists() or (worktree / "build.gradle.kts").exists():
                if which("gradle"):
                    commands.append(["gradle", "test"])
                elif (worktree / "gradlew").exists():
                    commands.append(["./gradlew", "test"])
                else:
                    raise ToolMissingError(
                        "gradle executable not found on PATH and no gradlew wrapper; "
                        "cannot run Android validation"
                    )
            # Note: we could also check for Android-specific test commands, but we'll stick to unit tests.

        # Unreal
        if project_type["unreal"]:
            # Unreal Engine projects have a .uproject file and source code.
            # We can try to run the build and test via UnrealBuildTool, but it's complex.
            # For now, we'll skip and rely on the user to set up custom test commands.
            # We'll just note that we don't have a test command for Unreal.
            pass

        return commands