"""Claude Code executor: a real ``claude -p`` headless invocation.

The adapter drives the installed Claude Code CLI in non-interactive
mode inside the task's isolated worktree. It uses
``--output-format stream-json`` so the UI receives the agent's own
progress events as they happen, and it never bypasses the permission
system with a blanket "skip everything" flag: instead it *allows* the
editing tools this architecture grants (Read/Edit/Write/Glob/Grep plus
bounded bash), which is a narrower grant than the dangerous variant.

Cancellation and timeouts kill the whole process group (see
``stream_command``), so a cancelled task cannot leave an agent
running against the repository.
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

ALLOWED_TOOLS = "Read,Edit,Write,MultiEdit,Glob,Grep,Bash(git status:*),Bash(ls:*)"


class ClaudeCodeExecutor(Executor):
    id = "claude-code"
    label = "Claude Code"
    kind = "cli"
    binary = "claude"
    capabilities = (
        "multi-file-implementation",
        "refactoring",
        "streaming-output",
        "tool-use",
    )

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary or "claude")
        if not path:
            return False, "claude CLI not found on PATH"
        return True, path

    def version(self) -> Optional[str]:
        path = shutil.which(self.binary or "claude")
        if not path:
            return None
        from app.workers.safe_subprocess import run

        result = run([path, "--version"], timeout=30)
        if result.get("returncode") == 0:
            return (result.get("stdout") or "").strip() or None
        return None

    def execute(self, ctx: ExecutionContext) -> ExecutionOutcome:
        path = shutil.which(self.binary or "claude")
        if not path:
            return ExecutionOutcome(
                succeeded=False,
                summary="claude CLI unavailable",
                error={"type": "ToolMissingError", "message": "claude not found"},
            )

        cmd = [
            path,
            "-p",
            ctx.prompt(),
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            "--allowedTools",
            ALLOWED_TOOLS,
            "--add-dir",
            str(ctx.worktree),
        ]

        try:
            stream = stream_command(cmd, ctx.worktree, ctx=ctx)
        except TaskCancelled:
            raise
        except FileNotFoundError as exc:
            return ExecutionOutcome(
                succeeded=False,
                summary="claude CLI could not be spawned",
                error={"type": "ToolMissingError", "message": str(exc)},
            )

        # Claude's stream-json is the agent's own event vocabulary;
        # surface the readable parts instead of dumping raw JSON.
        for line in stream.stdout.splitlines():
            _surface_json_event(line, ctx)

        touched = changed_files(ctx.worktree)
        error = None
        if stream.timed_out:
            error = {
                "type": "Timeout",
                "message": f"claude exceeded {ctx.timeout_seconds:.0f}s",
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
            summary=(
                "claude-code finished"
                if error is None
                else "claude-code failed"
            ),
            files_touched=touched,
            error=error,
            output={
                "duration_seconds": stream.duration_seconds,
                "stdout_tail": stream.stdout[-4000:],
                "stderr_tail": stream.stderr[-2000:],
            },
        )


def _surface_json_event(line: str, ctx: ExecutionContext) -> None:
    """Convert one stream-json line into a readable terminal line."""
    text = line.strip()
    if not text or not text.startswith("{"):
        if text:
            ctx.on_output("claude", text)
        return
    try:
        payload: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError:
        ctx.on_output("claude", text)
        return

    kind = payload.get("type")
    if kind == "assistant":
        for block in payload.get("message", {}).get("content", []) or []:
            if block.get("type") == "text" and block.get("text"):
                ctx.on_output("claude", block["text"].strip())
            elif block.get("type") == "tool_use":
                ctx.on_output("claude", f"→ {block.get('name')}")
    elif kind == "result":
        ctx.on_output("claude", f"done ({payload.get('subtype')})")
    elif kind == "system":
        ctx.on_output("claude", payload.get("subtype", "system"))
