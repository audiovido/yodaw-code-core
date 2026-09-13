"""Bounded, cancellable subprocess execution with process-group kill.

Every command runs in its own process group (start_new_session) so a
cancellation or timeout terminates the whole tree, not just the direct
child. Output capture is bounded to protect evidence from unbounded
logs. Cancellation is cooperative: callers pass cancel_check() which
raises MissionCancelled; the child group is killed before the exception
propagates.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable, Optional

from app.workers.mission_evidence import now_iso
from app.workers.worker_errors import MissionCancelled, SubprocessTimeout

DEFAULT_TIMEOUT = 300
MAX_OUTPUT_BYTES = 1_000_000
KILL_GRACE_SECONDS = 5
POLL_INTERVAL_SECONDS = 0.05


def which(tool: str) -> Optional[str]:
    """Return the absolute path of an executable on PATH, or None."""
    return shutil.which(tool)


def _spool(stream, sink: list, cap: threading.Event, cap_size: int) -> None:
    """Read a pipe into a bounded list; keep draining after the cap."""
    total = 0
    for chunk in iter(lambda: stream.read(65536), ""):
        if not cap.is_set() and total < cap_size:
            room = cap_size - total
            sink.append(chunk[:room])
            total += min(len(chunk), room)
            if total >= cap_size:
                cap.set()
    stream.close()


def _terminate_process_group(proc, grace: float = KILL_GRACE_SECONDS) -> None:
    """Terminate the child's whole process group, escalating to SIGKILL."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        proc.wait()


def run(
    cmd,
    cwd=None,
    timeout: float = DEFAULT_TIMEOUT,
    cancel_check: Optional[Callable[[], None]] = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    env=None,
) -> dict:
    """Execute a command as one bounded, cancellable unit.

    Returns a deterministic dict:
      cmd, cwd, stdout, stderr, returncode, timestamp,
      timed_out, output_truncated, cancelled

    - Process group termination: the child runs in its own session;
      timeout or cancellation kills the entire process group.
    - Bounded output: stdout/stderr are each capped at
      max_output_bytes and flagged via output_truncated.
    - Return-code honesty: the OS return code is reported as-is; a
      timeout leaves the post-kill returncode in place and sets
      timed_out instead of fabricating a pass.
    - Cancellation: cancel_check() runs during execution; when it
      raises MissionCancelled the process group is killed first, then
      the exception propagates.
    - A command that cannot be spawned raises FileNotFoundError so
      callers can map it to ToolMissingError.
    """
    environ = dict(os.environ)
    if env:
        environ.update(env)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
        env=environ,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_cap = threading.Event()
    stderr_cap = threading.Event()
    readers = [
        threading.Thread(
            target=_spool,
            args=(proc.stdout, stdout_chunks, stdout_cap, max_output_bytes),
            daemon=True,
        ),
        threading.Thread(
            target=_spool,
            args=(proc.stderr, stderr_chunks, stderr_cap, max_output_bytes),
            daemon=True,
        ),
    ]
    for thread in readers:
        thread.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    cancelled = False

    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                _terminate_process_group(proc)
                timed_out = True
                break
            if cancel_check is not None:
                cancel_check()
            time.sleep(POLL_INTERVAL_SECONDS)
    except MissionCancelled:
        _terminate_process_group(proc)
        for thread in readers:
            thread.join(timeout=KILL_GRACE_SECONDS)
        raise
    finally:
        for thread in readers:
            thread.join(timeout=KILL_GRACE_SECONDS)

    # After a normal exit, if the deadline raced the poll, the group
    # is already dead and timed_out already set above.
    return {
        "cmd": " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd),
        "cwd": str(cwd) if cwd else None,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
        "returncode": proc.returncode,
        "timestamp": now_iso(),
        "timed_out": timed_out,
        "output_truncated": stdout_cap.is_set() or stderr_cap.is_set(),
        "cancelled": cancelled,
    }


def run_with_timeout(cmd, cwd=None, timeout: float = DEFAULT_TIMEOUT, env=None) -> dict:
    """Run a command and raise SubprocessTimeout when it exceeds timeout."""
    result = run(cmd, cwd=cwd, timeout=timeout, env=env)
    if result["timed_out"]:
        raise SubprocessTimeout(
            "command exceeded %ss deadline: %s" % (result["cmd"], timeout)
        )
    return result