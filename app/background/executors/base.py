"""Executor contract: one normalized interface over every coding agent.

Adapters wrap *real* installed tools. Nothing here fakes an
integration: an executor that cannot find its binary reports
``available() == False`` with a diagnostic, and the registry simply
does not select it. The product must keep working when one agent is
absent (Codex is not installed on this machine, for example) —
availability degrades, the task does not.

The distinction that makes the whole architecture honest:

- an executor's job ends when it reports what it did to a real
  worktree (``ExecutionOutcome``)
- whether the task is *done* is decided only by the verifier

So ``ExecutionOutcome.succeeded`` is a statement about a process exit
code, never about acceptance criteria.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.background.models import (
    ExecutionOutcome,
    ExecutorInfo,
    PlanResult,
    TaskCancelled,
    TaskRecord,
    TaskTimeout,
    now_iso,
)
from app.workers.safe_subprocess import run as run_command

DEFAULT_EXECUTOR_TIMEOUT = 900
STREAM_KILL_GRACE_SECONDS = 5.0


@dataclass
class ExecutionContext:
    """Everything an executor needs, scoped to one isolated worktree."""

    task: TaskRecord
    repo: Path
    worktree: Path
    plan: PlanResult
    timeout_seconds: float = DEFAULT_EXECUTOR_TIMEOUT
    cancel_check: Callable[[], None] = lambda: None
    on_output: Callable[[str, str], None] = lambda stream, line: None
    on_pid: Callable[[Optional[int]], None] = lambda pid: None
    env: dict[str, str] = field(default_factory=dict)

    def prompt(self) -> str:
        return build_executor_prompt(self.task, self.plan)


@dataclass
class StreamResult:
    exit_code: Optional[int]
    stdout: str
    stderr: str
    lines: list[tuple[str, str]]
    timed_out: bool
    cancelled: bool
    duration_seconds: float


def build_executor_prompt(task: TaskRecord, plan: PlanResult) -> str:
    """The bounded, evidence-oriented brief handed to a CLI agent.

    It states the goal, the plan Grok produced, and the acceptance
    criteria — and nothing else. The worktree is the working
    directory, so no path from outside the sandbox is ever named.
    """
    acceptance = "\n".join(f"- {item}" for item in plan.acceptance)
    steps = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan.plan))
    scope = ""
    if plan.target_files:
        scope = (
            "\nExpected files (create or modify only these unless the task "
            "requires otherwise):\n"
            + "\n".join(f"- {path}" for path in plan.target_files)
        )
    return (
        "You are working inside an isolated git worktree. Make the "
        "changes directly on disk in the current directory. Do not "
        "create git branches, do not commit, do not push, and do not "
        "touch anything outside this directory.\n\n"
        f"Task: {task.goal}\n\n"
        f"Plan:\n{steps}\n\n"
        f"Acceptance criteria:\n{acceptance}\n"
        f"{scope}\n\n"
        "When finished, leave the working tree containing the required "
        "changes. Do not print a summary instead of doing the work."
    )


def stream_command(
    cmd: list[str],
    cwd: Path,
    *,
    ctx: ExecutionContext,
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
) -> StreamResult:
    """Run a CLI agent as one bounded, cancellable, streamed process.

    Mirrors ``app.workers.safe_subprocess`` semantics (own process
    group, group kill on timeout/cancel, bounded capture) but adds
    line-level streaming so the UI can show real executor output while
    it happens, plus PID reporting for the task record.
    """
    effective_timeout = ctx.timeout_seconds if timeout is None else timeout
    environ = dict(os.environ)
    environ.update(ctx.env)
    if env:
        environ.update(env)

    started = time.monotonic()
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        bufsize=1,
        start_new_session=True,
        env=environ,
    )
    ctx.on_pid(process.pid)

    lines: list[tuple[str, str]] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    lock = threading.Lock()

    def pump(stream_name: str, pipe) -> None:
        try:
            for raw in iter(pipe.readline, ""):
                line = raw.rstrip("\n")
                with lock:
                    lines.append((stream_name, line))
                    if stream_name == "stderr":
                        stderr_parts.append(line)
                    else:
                        stdout_parts.append(line)
                try:
                    ctx.on_output(stream_name, line)
                except Exception:
                    # A broken listener must never kill the executor.
                    pass
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    readers = [
        threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = started + effective_timeout
    timed_out = False
    cancelled = False
    try:
        while process.poll() is None:
            if time.monotonic() >= deadline:
                _terminate_group(process)
                timed_out = True
                break
            try:
                ctx.cancel_check()
            except TaskCancelled:
                _terminate_group(process)
                cancelled = True
                raise
            except TaskTimeout:
                _terminate_group(process)
                timed_out = True
                break
            time.sleep(0.1)
    finally:
        for reader in readers:
            reader.join(timeout=STREAM_KILL_GRACE_SECONDS)
        ctx.on_pid(None)

    return StreamResult(
        exit_code=process.returncode,
        stdout="\n".join(stdout_parts),
        stderr="\n".join(stderr_parts),
        lines=lines,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_seconds=round(time.monotonic() - started, 2),
    )


def _terminate_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=STREAM_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            process.wait(timeout=STREAM_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass


def changed_files(worktree: Path) -> list[str]:
    """Real, filesystem-derived change list for a worktree."""
    result = run_command(["git", "status", "--porcelain"], cwd=str(worktree))
    files: list[str] = []
    for line in (result.get("stdout") or "").splitlines():
        entry = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in entry:
            entry = entry.split(" -> ")[-1].strip()
        if entry:
            files.append(entry.strip('"'))
    return files


class Executor(ABC):
    """The one interface every coding agent implements."""

    id: str = "unnamed"
    label: str = "Unnamed"
    kind: str = "cli"
    capabilities: tuple[str, ...] = ()
    binary: Optional[str] = None

    # ----------------------------------------------------- descriptor
    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """``(available, detail)`` — probes the real installation."""

    def info(self) -> ExecutorInfo:
        available, detail = self.available()
        return ExecutorInfo(
            id=self.id,
            label=self.label,
            kind=self.kind,
            available=available,
            detail=detail,
            capabilities=list(self.capabilities),
            command=self.binary,
            version=self.version() if available else None,
        )

    def version(self) -> Optional[str]:
        return None

    # -------------------------------------------------------- execute
    @abstractmethod
    def execute(self, ctx: ExecutionContext) -> ExecutionOutcome:
        """Perform real work inside ``ctx.worktree``."""

    def cancel(self) -> None:
        """Best-effort cancel of the current run (stream_command kills
        the process group; adapters with extra handles override this)."""

    def status(self) -> str:
        available, _ = self.available()
        return "READY" if available else "UNAVAILABLE"


def _now() -> str:
    return now_iso()
