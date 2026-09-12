from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from app.workers.base import Worker, WorkerResult
from app.workers.python_runtime import resolve_python_executable
from app.workers.repo_code_worker import CancelContext

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def run(cmd, cwd=None, timeout=120):
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

class CodeWorker(Worker):
    """Deterministic local worker for the 'code' capability."""

    name = "code-bud"
    capabilities = {"code"}

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

    def execute(self, goal: str, metadata: Optional[dict] = None) -> WorkerResult:
        # Cancellation check at the very start if we have the necessary metadata
        metadata = metadata or {}
        if metadata.get("mission_id") and metadata.get("_event_store"):
            try:
                ctx = CancelContext(metadata["mission_id"], metadata["_event_store"])
                ctx.cancel_check("code_worker_start")
            except Exception:
                # If we can't create the context, we just proceed without cancellation check
                pass

        base = Path("workspace")
        base.mkdir(exist_ok=True)

        workdir = Path(tempfile.mkdtemp(prefix="yodaw_", dir=base))
        evidence = []

        try:
            # -------------------------------------------------
            # 1) Create isolated git repo
            # -------------------------------------------------
            evidence.append(run(["git", "init"], cwd=workdir))

            evidence.append(
                run(
                    ["git", "config", "user.email", "yodaw@local"],
                    cwd=workdir,
                )
            )

            evidence.append(
                run(
                    ["git", "config", "user.name", "YODAW Code Bud"],
                    cwd=workdir,
                )
            )

            # -------------------------------------------------
            # 2) Create deterministic sample project
            # -------------------------------------------------
            (workdir / "mathlib.py").write_text(
                "def add(a, b):\n"
                "    return a + b\n"
            )

            (workdir / "test_mathlib.py").write_text(
                "from mathlib import add\n\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n"
            )

            evidence.append(run(["git", "add", "."], cwd=workdir))

            evidence.append(
                run(
                    ["git", "commit", "-m", "baseline sample project"],
                    cwd=workdir,
                )
            )

            # -------------------------------------------------
            # 3) Perform real code modification
            # -------------------------------------------------
            with (workdir / "mathlib.py").open("a") as f:
                f.write(
                    "\n\ndef multiply(a, b):\n"
                    "    return a * b\n"
                )

            with (workdir / "test_mathlib.py").open("a") as f:
                f.write(
                    "\n\ndef test_multiply():\n"
                    "    assert multiply(4, 5) == 20\n"
                )

            # Fix import deterministically
            test_file = workdir / "test_mathlib.py"
            content = test_file.read_text()
            content = content.replace(
                "from mathlib import add",
                "from mathlib import add, multiply",
            )
            test_file.write_text(content)

            # -------------------------------------------------
            # 4) Run tests
            # -------------------------------------------------
            test_result = run(
                [resolve_python_executable(), "-m", "pytest", "-q"],
                cwd=workdir,
            )
            evidence.append(test_result)

            success = test_result["returncode"] == 0

            # -------------------------------------------------
            # 5) Diff evidence
            # -------------------------------------------------
            diff_result = run(
                ["git", "diff"],
                cwd=workdir,
            )
            evidence.append(diff_result)

            # -------------------------------------------------
            # 6) Commit only after validation PASS
            # -------------------------------------------------
            commit_sha = None

            if success:
                evidence.append(run(["git", "add", "."], cwd=workdir))
                evidence.append(
                    run(
                        ["git", "commit", "-m", "add multiply function"],
                        cwd=workdir,
                    )
                )

                sha_result = run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=workdir,
                )
                evidence.append(sha_result)
                commit_sha = sha_result["stdout"].strip()

            status_result = run(
                ["git", "status", "--short"],
                cwd=workdir,
            )
            evidence.append(status_result)

            return WorkerResult(
                success=success,
                output={
                    "goal": goal,
                    "workspace": str(workdir),
                    "tests_passed": success,
                    "commit_sha": commit_sha,
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
                    "workspace": str(workdir),
                },
                evidence=evidence,
                error={
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                retryable=False,
            )