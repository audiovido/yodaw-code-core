"""Focused tests for safe_subprocess: cancellation, timeout, process-group
termination, bounded output, and return-code honesty."""

import os
import sys
import time

import pytest

from app.workers.safe_subprocess import (
    MAX_OUTPUT_BYTES,
    run,
    run_with_timeout,
    which,
)
from app.workers.worker_errors import MissionCancelled, SubprocessTimeout

PYTHON = sys.executable


def _is_running(cmd_fragment: str) -> bool:
    for _ in range(40):
        result = os.popen("pgrep -f %s" % cmd_fragment).read().strip()
        if not result:
            return False
        time.sleep(0.05)
    return True


def test_return_code_honesty():
    result = run([PYTHON, "-c", "import sys; sys.exit(3)"])
    assert result["returncode"] == 3
    assert result["timed_out"] is False
    assert result["cancelled"] is False
    assert isinstance(result["cmd"], str)
    assert result["timestamp"]


def test_success_returns_zero_and_output():
    result = run([PYTHON, "-c", "print('hello world')"])
    assert result["returncode"] == 0
    assert result["stdout"].strip() == "hello world"
    assert result["timed_out"] is False


def test_timeout_terminates_process_group():
    cmd = [
        PYTHON,
        "-c",
        "import subprocess, time;"
        "subprocess.Popen(['sleep', '12345']);"
        "time.sleep(12345)",
    ]
    started = time.monotonic()
    result = run(cmd, timeout=0.3)
    elapsed = time.monotonic() - started

    assert result["timed_out"] is True
    assert result["returncode"] != 0
    assert elapsed < 15, "timeout did not bound the call"
    # The spawned 'sleep 12345' grandchild must be dead too.
    assert not _is_running("sleep 12345")


def test_cancellation_during_process_kills_group_and_propagates():
    calls = {"n": 0}

    def cancel_check():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise MissionCancelled("probe")

    cmd = [
        PYTHON,
        "-c",
        "import subprocess, time;"
        "subprocess.Popen(['sleep', '12346']);"
        "time.sleep(12346)",
    ]

    with pytest.raises(MissionCancelled) as excinfo:
        run(cmd, cancel_check=cancel_check, timeout=30)

    assert excinfo.value.at == "probe"
    assert not _is_running("sleep 12346")


def test_bounded_output():
    cmd = [PYTHON, "-c", "print('x' * 200_000)"]
    result = run(cmd, max_output_bytes=4096)
    assert result["output_truncated"] is True
    assert len(result["stdout"]) <= 4096
    assert result["returncode"] == 0

    big = run([PYTHON, "-c", "print('x' * 200_000)"])
    assert big["output_truncated"] is False
    assert big["stdout"] == "x" * 200_000 + "\n"


def test_which_detects_and_misses():
    assert which("python3") or which(PYTHON.split("/")[-1])
    assert which("definitely_not_a_real_tool_xyz_123") is None


def test_spawn_failure_propagates_file_not_found():
    with pytest.raises(FileNotFoundError):
        run(["/definitely/not/a/real/tool"])


def test_result_dict_is_deterministic_shape():
    result = run([PYTHON, "-c", "pass"])
    assert list(result.keys()) == [
        "cmd",
        "cwd",
        "stdout",
        "stderr",
        "returncode",
        "timestamp",
        "timed_out",
        "output_truncated",
        "cancelled",
    ]


def test_run_with_timeout_raises():
    with pytest.raises(SubprocessTimeout):
        run_with_timeout([PYTHON, "-c", "import time; time.sleep(60)"], timeout=0.4)


def test_run_with_timeout_passes_on_success():
    result = run_with_timeout([PYTHON, "-c", "pass"], timeout=10)
    assert result["returncode"] == 0