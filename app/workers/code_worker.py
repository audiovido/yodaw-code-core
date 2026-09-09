import subprocess
from pathlib import Path
from datetime import datetime, timezone

from app.workers.base import Worker, WorkerResult


class CodeWorker(Worker):
    name = "code-bud"
    capabilities = {"code"}

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

    def execute(self, goal: str) -> WorkerResult:
        workspace = Path("workspace")
        workspace.mkdir(exist_ok=True)

        evidence = []

        # v0 deliberately safe:
        # proves execution/evidence pipeline before autonomous mutation.
        proc = subprocess.run(
            [
                "python",
                "-c",
                "print('YODAW Code Bud execution pipeline OK')",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        evidence.append(
            {
                "type": "command",
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
                "returncode": proc.returncode,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        return WorkerResult(
            success=proc.returncode == 0,
            output={
                "goal": goal,
                "message": "Code worker execution pipeline is operational.",
            },
            evidence=evidence,
            retryable=False,
        )
