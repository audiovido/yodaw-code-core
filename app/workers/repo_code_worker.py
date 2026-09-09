import os
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from app.workers.base import Worker, WorkerResult


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


class RepoCodeWorker(Worker):
    name = "repo-code-bud"
    capabilities = {"repo-code"}

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

    def execute(self, goal: str) -> WorkerResult:
        evidence = []

        repo_path = os.environ.get("YODAW_TARGET_REPO")
        if not repo_path:
            return WorkerResult(
                success=False,
                output={},
                evidence=[],
                error={
                    "type": "ConfigError",
                    "message": "YODAW_TARGET_REPO is not set",
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

        branch_name = f"yodaw/task-{int(datetime.now().timestamp())}"
        worktree = Path(
            tempfile.mkdtemp(prefix="repo_", dir=worktree_root)
        )

        # tempfile creates the dir; git worktree wants target absent
        worktree.rmdir()

        try:
            evidence.append(
                run(
                    ["git", "status", "--short"],
                    cwd=repo,
                )
            )

            if evidence[-1]["stdout"].strip():
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

            evidence.append(
                run(
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
            )

            # Basic inspection
            evidence.append(
                run(
                    ["git", "status", "--short"],
                    cwd=worktree,
                )
            )

            evidence.append(
                run(
                    ["find", ".", "-maxdepth", "2", "-type", "f"],
                    cwd=worktree,
                )
            )

            # This stage proves repo isolation + validation.
            # No autonomous code mutation yet.
            test_commands = []

            if (worktree / "pytest.ini").exists() or \
               (worktree / "tests").exists() or \
               (worktree / "pyproject.toml").exists():
                test_commands.append(
                    ["python", "-m", "pytest", "-q"]
                )

            if (worktree / "package.json").exists():
                test_commands.append(
                    ["npm", "test", "--", "--runInBand"]
                )

            test_results = []

            for cmd in test_commands:
                result = run(cmd, cwd=worktree)
                evidence.append(result)
                test_results.append(result)

            tests_passed = all(
                r["returncode"] == 0
                for r in test_results
            ) if test_results else True

            diff_result = run(
                ["git", "diff"],
                cwd=worktree,
            )
            evidence.append(diff_result)

            status_result = run(
                ["git", "status", "--short"],
                cwd=worktree,
            )
            evidence.append(status_result)

            return WorkerResult(
                success=tests_passed,
                output={
                    "goal": goal,
                    "repo": str(repo),
                    "worktree": str(worktree),
                    "branch": branch_name,
                    "base_sha": base_sha["stdout"].strip(),
                    "tests_detected": len(test_commands),
                    "tests_passed": tests_passed,
                    "working_tree_clean": status_result["stdout"].strip() == "",
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
                },
                evidence=evidence,
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                retryable=False,
            )
