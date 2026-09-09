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
            # Deterministic edit contract for Stage 4
            # -------------------------------------------------
            target_file = metadata.get("target_file")
            find_text = metadata.get("find")
            replace_text = metadata.get("replace")

            # ---------------------------------------------
            # Stage 5:
            # If explicit deterministic edit is absent,
            # ask the local Coder Brain to plan the edit.
            # ---------------------------------------------
            llm_plan = None

            if (
                not target_file
                or find_text is None
                or replace_text is None
            ):
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

                target_file = llm_plan["target_file"]
                find_text = llm_plan["find"]
                replace_text = llm_plan["replace"]

            target = (worktree / target_file).resolve()

            if worktree.resolve() not in target.parents:
                return WorkerResult(
                    success=False,
                    output={},
                    evidence=evidence,
                    error={
                        "type": "PathEscapeError",
                        "message": "target_file escapes isolated worktree",
                    },
                    retryable=False,
                )

            if not target.exists():
                return WorkerResult(
                    success=False,
                    output={
                        "target_file": str(target),
                    },
                    evidence=evidence,
                    error={
                        "type": "TargetNotFound",
                        "message": f"{target_file} does not exist",
                    },
                    retryable=False,
                )

            original = target.read_text()

            actual_find_text = find_text

            if find_text not in original:
                actual_find_text = find_relaxed_unique_match(
                    original,
                    find_text,
                )

                if actual_find_text is not None:
                    evidence.append(
                        {
                            "type": "relaxed_match",
                            "file": target_file,
                            "requested_find": find_text,
                            "actual_find": actual_find_text,
                            "timestamp": now_iso(),
                        }
                    )

            if actual_find_text is None or actual_find_text not in original:
                return WorkerResult(
                    success=False,
                    output={
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

            modified = original.replace(
                actual_find_text,
                replace_text,
                1,
            )
            target.write_text(modified)

            evidence.append(
                {
                    "type": "edit",
                    "file": target_file,
                    "find": find_text,
                    "replace": replace_text,
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

                if retries >= max_retries:
                    break

                retries += 1

                # Stage 4 recovery:
                # revert edit if validation failed.
                target.write_text(original)

                evidence.append(
                    {
                        "type": "recovery",
                        "action": "revert_failed_edit",
                        "retry": retries,
                        "timestamp": now_iso(),
                    }
                )

                # No autonomous second edit yet.
                # Stage 5 LLM planner/fixer will handle it.
                break

            diff_result = run(
                ["git", "diff", "--", target_file],
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
                ["git", "add", "--", target_file],
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
                    "target_file": target_file,
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
