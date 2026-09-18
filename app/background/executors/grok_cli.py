"""Grok CLI executor: a real headless ``grok`` invocation.

Grok is primarily the architect in this architecture, but it is also a
legitimate executor for research / architecture-heavy work. This
adapter runs the installed Grok CLI headlessly inside the task
worktree with ``--always-approve`` (it is already sandboxed by the
isolated worktree) and surfaces its output as terminal lines.

Availability is honest: if the CLI exists but its configured route
refuses the request (for example a provider 403), the executor still
reports available, and the *run* fails with the provider's real error
— the verifier then refuses to pass. Nothing is reported as success
because a CLI was merely present.
"""

from __future__ import annotations

import shutil
from typing import Optional

from app.background.executors.base import (
    ExecutionContext,
    Executor,
    changed_files,
    stream_command,
)
from app.background.models import ExecutionOutcome, TaskCancelled


class GrokCliExecutor(Executor):
    id = "grok-cli"
    label = "Grok CLI"
    kind = "cli"
    binary = "grok"
    capabilities = (
        "architecture",
        "research",
        "repo-analysis",
        "multi-file-implementation",
    )

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary or "grok")
        if not path:
            return False, "grok CLI not found on PATH"
        return True, path

    def version(self) -> Optional[str]:
        path = shutil.which(self.binary or "grok")
        if not path:
            return None
        from app.workers.safe_subprocess import run

        result = run([path, "--version"], timeout=30)
        if result.get("returncode") == 0:
            return (result.get("stdout") or "").strip() or None
        return None

    def execute(self, ctx: ExecutionContext) -> ExecutionOutcome:
        path = shutil.which(self.binary or "grok")
        if not path:
            return ExecutionOutcome(
                succeeded=False,
                summary="grok CLI unavailable",
                error={"type": "ToolMissingError", "message": "grok not found"},
            )

        cmd = [
            path,
            "-p",
            ctx.prompt(),
            "--cwd",
            str(ctx.worktree),
            "--output-format",
            "plain",
            "--permission-mode",
            "bypassPermissions",
            "--always-approve",
            "--no-subagents",
            "--max-turns",
            "30",
        ]

        try:
            stream = stream_command(cmd, ctx.worktree, ctx=ctx)
        except TaskCancelled:
            raise
        except FileNotFoundError as exc:
            return ExecutionOutcome(
                succeeded=False,
                summary="grok CLI could not be spawned",
                error={"type": "ToolMissingError", "message": str(exc)},
            )

        touched = changed_files(ctx.worktree)
        error = None
        if stream.timed_out:
            error = {
                "type": "Timeout",
                "message": f"grok exceeded {ctx.timeout_seconds:.0f}s",
            }
        elif stream.exit_code not in (0, None):
            error = {
                "type": "ExecutorFailed",
                "message": (stream.stderr or stream.stdout or "")[-2000:],
                "exit_code": stream.exit_code,
            }

        return ExecutionOutcome(
            succeeded=error is None,
            exit_code=stream.exit_code,
            summary="grok-cli finished" if error is None else "grok-cli failed",
            files_touched=touched,
            error=error,
            output={
                "duration_seconds": stream.duration_seconds,
                "stdout_tail": stream.stdout[-4000:],
                "stderr_tail": stream.stderr[-2000:],
            },
        )
