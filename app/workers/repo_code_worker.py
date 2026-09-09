import os
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from app.workers.base import Worker, WorkerResult
from app.llm.coder import generate_edit_plan


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
            # Atomic multi-edit contract
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

            # ---------------------------------------------
            # If deterministic edits are absent,
            # ask the Coder Brain for a structured plan.
            # ---------------------------------------------
            llm_plan = None

            if explicit_edits is None:
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

                # The Coder Brain may return either the structured
                # multi-edit plan or the legacy Stage 6 single-edit
                # shape; normalize the legacy shape at this boundary.
                edits = llm_plan.get("edits")

                if edits is None:
                    legacy_target = llm_plan.get("target_file")
                    legacy_find = llm_plan.get("find")
                    legacy_replace = llm_plan.get("replace")

                    if (
                        legacy_target
                        and legacy_find is not None
                        and legacy_replace is not None
                    ):
                        edits = [
                            {
                                "target_file": legacy_target,
                                "find": legacy_find,
                                "replace": legacy_replace,
                            }
                        ]

            else:
                edits = explicit_edits

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

            # -------------------------------------------------
            # Validate and stage ALL edits in memory first.
            #
            # Nothing is written until every path and every
            # find/replace operation has been validated.
            # -------------------------------------------------
            staged_contents = {}
            original_contents = {}
            prepared_edits = []
            touched_files = []

            for index, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    return WorkerResult(
                        success=False,
                        output={"edit_index": index},
                        evidence=evidence,
                        error={
                            "type": "InvalidEditPlan",
                            "message": f"Edit {index} is not an object",
                        },
                        retryable=False,
                    )

                required = ("target_file", "find", "replace")
                missing = [
                    key
                    for key in required
                    if key not in edit
                ]

                if missing:
                    return WorkerResult(
                        success=False,
                        output={"edit_index": index},
                        evidence=evidence,
                        error={
                            "type": "InvalidEditPlan",
                            "message": (
                                f"Edit {index} missing fields: {missing}"
                            ),
                        },
                        retryable=False,
                    )

                target_file = edit["target_file"]
                find_text = edit["find"]
                replace_text = edit["replace"]

                target = (worktree / target_file).resolve()

                if worktree.resolve() not in target.parents:
                    return WorkerResult(
                        success=False,
                        output={
                            "edit_index": index,
                            "target_file": target_file,
                        },
                        evidence=evidence,
                        error={
                            "type": "PathEscapeError",
                            "message": (
                                f"{target_file} escapes isolated worktree"
                            ),
                        },
                        retryable=False,
                    )

                if not target.exists():
                    return WorkerResult(
                        success=False,
                        output={
                            "edit_index": index,
                            "target_file": str(target),
                        },
                        evidence=evidence,
                        error={
                            "type": "TargetNotFound",
                            "message": f"{target_file} does not exist",
                        },
                        retryable=False,
                    )

                if not target.is_file():
                    return WorkerResult(
                        success=False,
                        output={
                            "edit_index": index,
                            "target_file": str(target),
                        },
                        evidence=evidence,
                        error={
                            "type": "TargetNotFile",
                            "message": f"{target_file} is not a file",
                        },
                        retryable=False,
                    )

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

                if (
                    actual_find_text is None
                    or actual_find_text not in current
                ):
                    return WorkerResult(
                        success=False,
                        output={
                            "edit_index": index,
                            "target_file": target_file,
                        },
                        evidence=evidence,
                        error={
                            "type": "FindTextMissing",
                            "message": (
                                "Requested source text was not found "
                                "as an exact or unique relaxed match"
                            ),
                        },
                        retryable=False,
                    )

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

            # -------------------------------------------------
            # Atomic apply:
            # every edit is known-valid before first write.
            # -------------------------------------------------
            for target_file in touched_files:
                target = (worktree / target_file).resolve()
                target.write_text(staged_contents[target_file])

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

            test_commands = detect_test_commands(worktree)

            tests_passed = True
            last_test_results = []

            while True:
                last_test_results = []

                for cmd in test_commands:
                    result = run(cmd, cwd=worktree)
                    evidence.append(result)
                    last_test_results.append(result)

                tests_passed = all(
                    item["returncode"] == 0
                    for item in last_test_results
                ) if last_test_results else True

                if tests_passed:
                    break

                # Stage 4 recovery:
                # revert ALL edits if validation failed by restoring
                # every touched file's original text. This must run
                # regardless of the remaining retry budget so no
                # half-validated change is ever left behind.
                for edited_file, original_text in original_contents.items():
                    edited_path = (worktree / edited_file).resolve()
                    edited_path.write_text(original_text)

                evidence.append(
                    {
                        "type": "recovery",
                        "action": "revert_failed_edits",
                        "files": list(original_contents.keys()),
                        "retry": retries,
                        "timestamp": now_iso(),
                    }
                )

                if retries >= max_retries:
                    break

                retries += 1

                # No autonomous corrective edits yet.
                # Stage 7.3 will feed failure evidence back to the
                # LLM for bounded corrective retries.
                break

            diff_result = run(
                ["git", "diff", "--", *touched_files],
                cwd=worktree,
            )
            evidence.append(diff_result)

            if not tests_passed:
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
                        "diff": diff_result["stdout"],
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
                    "tests_detected": len(test_commands),
                    "tests_passed": True,
                    "retries": retries,
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
