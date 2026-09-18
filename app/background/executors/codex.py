"""Codex executor: a real ``codex exec`` invocation.

Codex is the deep-debugging / repo-analysis executor. The adapter is
faithful to the real CLI surface (``codex exec --json`` with a
sandboxed write mode), and it is honest about absence: on a machine
where the codex binary is not installed, ``available()`` reports
``False`` with the reason, the registry never selects it, and the
product keeps working.

Nothing here fabricates a run. If codex cannot be spawned the outcome
is a failure with a diagnostic, never a pass.
"""

from __future__ import annotations

import json
import shutil
from typing import Any, Optional

from app.background.executors.base import (
    ExecutionContext,
    Executor,
    changed_files,
    stream_command,
)
from app.background.models import ExecutionOutcome, TaskCancelled


class CodexExecutor(Executor):
    id = "codex"
    label = "Codex"
    kind = "cli"
    binary = "codex"
    capabilities = (
        "debugging",
        "repo-analysis",
        "deep-reasoning",
    )

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary or "codex")
        if not path:
            return False, "codex CLI not installed on PATH"
        return True, path

    def version(self) -> Optional[str]:
        path = shutil.which(self.binary or "codex")
        if not path:
            return None
        from app.workers.safe_subprocess import run

        result = run([path, "--version"], timeout=30)
        if result.get("returncode") == 0:
            return (result.get("stdout") or "").strip() or None
        return None

    def execute(self, ctx: ExecutionContext) -> ExecutionOutcome:
        path = shutil.which(self.binary or "codex")
        if not path:
            return ExecutionOutcome(
                succeeded=False,
                summary="codex CLI unavailable",
                error={
                    "type": "ToolMissingError",
                    "message": "codex is not installed on this machine",
                },
            )

        cmd = [
            path,
            "exec",
            "--cd",
            str(ctx.worktree),
            "--json",
            "--sandbox",
            "workspace-write",
            ctx.prompt(),
        ]

        try:
            stream = stream_command(cmd, ctx.worktree, ctx=ctx)
        except TaskCancelled:
            raise
        except FileNotFoundError as exc:
            return ExecutionOutcome(
                succeeded=False,
                summary="codex CLI could not be spawned",
                error={"type": "ToolMissingError", "message": str(exc)},
            )

        for line in stream.stdout.splitlines():
            _surface_json_event(line, ctx)

        touched = changed_files(ctx.worktree)
        error = None
        if stream.timed_out:
            error = {
                "type": "Timeout",
                "message": f"codex exceeded {ctx.timeout_seconds:.0f}s",
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
            summary="codex finished" if error is None else "codex failed",
            files_touched=touched,
            error=error,
            output={
                "duration_seconds": stream.duration_seconds,
                "stdout_tail": stream.stdout[-4000:],
                "stderr_tail": stream.stderr[-2000:],
            },
        )


def _surface_json_event(line: str, ctx: ExecutionContext) -> None:
    text = line.strip()
    if not text:
        return
    if not text.startswith("{"):
        ctx.on_output("codex", text)
        return
    try:
        payload: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        ctx.on_output("codex", text)
        return
    kind = payload.get("type") or payload.get("msg", {}).get("type")
    if kind:
        message = payload.get("msg", {}).get("message") or payload.get("message")
        ctx.on_output("codex", f"{kind}{': ' + str(message) if message else ''}")
