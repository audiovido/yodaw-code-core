"""Validation tooling detection and test-command execution.

Detection is language-aware (Python, JS/TS, Go, Rust, Java, C/C++,
C#, Swift, Bash, SQL) and deterministic: a fixed priority order maps
repo markers to concrete test commands. A selected tool that is not
installed fails fast with ToolMissingError; a repo with no detectable
test runner is treated as "no tests" and passes vacuously.

Timeout/failure interpretation is explicit: a timed-out result is a
failure, never a pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from app.workers.python_runtime import resolve_python_executable
from app.workers.safe_subprocess import run, which
from app.workers.worker_errors import ToolMissingError

PYTEST_CMD_TEMPLATE = ["-B", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

SQL_SYNTAX_CHECK = (
    "import sqlite3, sys, pathlib;"
    "con = sqlite3.connect(':memory:');"
    "ok = True;"
    "for f in sys.argv[1:]:"
    "    try:"
    "        con.executescript(pathlib.Path(f).read_text(errors='replace') + '\\n');"
    "    except sqlite3.Error as e:"
    "        print(f'{f}: {e}', file=sys.stderr); ok = False;"
    "sys.exit(0 if ok else 1)"
)


def _has_python_test_files(worktree: Path) -> bool:
    """True when any python test file exists under the worktree."""
    for path in worktree.rglob("*.py"):
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            return True

    return False


def detect_test_commands(
    worktree,
    *,
    python_executable: Optional[str] = None,
    tool_check: Callable[[str], Optional[str]] = which,
) -> list:
    """Return the ordered list of validation commands for a worktree.

    Raises ToolMissingError when a repository marker selects a runner
    that is not installed. Returns [] when no test runner applies.
    """
    worktree = Path(worktree)
    commands: list = []

    # Python
    python_markers = ("pytest.ini", "tests", "pyproject.toml", "setup.py")
    if any((worktree / marker).exists() for marker in python_markers):
        commands.append(
            [
                python_executable or resolve_python_executable(),
                *PYTEST_CMD_TEMPLATE,
            ]
        )
    elif _has_python_test_files(worktree):
        # A repository with test_*.py / *_test.py files still runs
        # pytest even without a marker file; a missing interpreter
        # fails explicitly during execution, never silently.
        commands.append(
            [
                python_executable or resolve_python_executable(),
                *PYTEST_CMD_TEMPLATE,
            ]
        )

    # JS/TS
    if (worktree / "package.json").exists():
        if not tool_check("npm"):
            raise ToolMissingError(
                "npm executable not found on PATH; "
                "cannot run JavaScript validation"
            )
        commands.append(["npm", "test", "--", "--runInBand"])

    # Go
    if (worktree / "go.mod").exists():
        if not tool_check("go"):
            raise ToolMissingError(
                "go executable not found on PATH; "
                "cannot run Go validation"
            )
        commands.append(["go", "test", "./..."])

    # Rust
    if (worktree / "Cargo.toml").exists():
        if not tool_check("cargo"):
            raise ToolMissingError(
                "cargo executable not found on PATH; "
                "cannot run Rust validation"
            )
        commands.append(["cargo", "test", "--quiet"])

    # Java
    if (worktree / "pom.xml").exists():
        if not tool_check("mvn"):
            raise ToolMissingError(
                "mvn executable not found on PATH; "
                "cannot run Java validation"
            )
        commands.append(["mvn", "-q", "test"])
    elif (worktree / "build.gradle").exists() or (worktree / "build.gradle.kts").exists():
        if not tool_check("gradle"):
            raise ToolMissingError(
                "gradle executable not found on PATH; "
                "cannot run JVM validation"
            )
        commands.append(["gradle", "-q", "test"])

    # C/C++
    if (worktree / "CMakeLists.txt").exists():
        if not tool_check("ctest"):
            raise ToolMissingError(
                "ctest executable not found on PATH; "
                "cannot run C/C++ validation"
            )
        commands.append(["ctest", "--test-dir", "."])
    elif (worktree / "Makefile").exists():
        if not tool_check("make"):
            raise ToolMissingError(
                "make executable not found on PATH; "
                "cannot run C/C++ validation"
            )
        commands.append(["make", "test"])

    # C#
    if any(worktree.glob("*.csproj")) or any(worktree.glob("*.sln")):
        if not tool_check("dotnet"):
            raise ToolMissingError(
                "dotnet executable not found on PATH; "
                "cannot run C# validation"
            )
        commands.append(["dotnet", "test"])

    # Swift
    if (worktree / "Package.swift").exists():
        if not tool_check("swift"):
            raise ToolMissingError(
                "swift executable not found on PATH; "
                "cannot run Swift validation"
            )
        commands.append(["swift", "test"])

    # Bash syntax checks
    sh_files = sorted(
        path for path in worktree.rglob("*.sh") if path.is_file()
    )
    if sh_files:
        shell = tool_check("bash") or tool_check("sh")
        if shell is None:
            raise ToolMissingError(
                "bash/sh executable not found on PATH; "
                "cannot run shell validation"
            )
        commands.append(
            [shell, "-n", *(str(p) for p in sh_files[:40])]
        )

    # SQL syntax checks via the stdlib sqlite3 module.
    sql_files = sorted(
        path for path in worktree.rglob("*.sql") if path.is_file()
    )
    if sql_files:
        commands.append(
            [
                python_executable or resolve_python_executable(),
                "-c",
                SQL_SYNTAX_CHECK,
                *(str(p) for p in sql_files[:40]),
            ]
        )

    return commands


def _pytest_zero_actual_tests(output: str) -> bool:
    """Return True if pytest output indicates zero passed and zero failed tests.

    This catches the case where all tests are skipped (returncode 0)
    but no actual test assertions ran.
    """
    import re
    # Look for patterns like "N passed", "M failed"
    passed_match = re.search(r'(\d+)\s+passed', output)
    failed_match = re.search(r'(\d+)\s+failed', output)
    passed_count = int(passed_match.group(1)) if passed_match else 0
    failed_count = int(failed_match.group(1)) if failed_match else 0
    return passed_count == 0 and failed_count == 0 and ('passed' in output or 'failed' in output)


def run_validation(
    worktree,
    evidence: list,
    test_commands: list,
    *,
    timeout: float = 300,
    cancel_check: Optional[Callable[[], None]] = None,
    max_output_bytes: int = 1_000_000,
):
    """Run every detected command once; return (passed, results).

    - A command that cannot be spawned raises ToolMissingError with a
      clear diagnostic (never a silent skip).
    - No commands => passed True (no tests to fail).
    - A timed-out or non-zero result is a failure.
    - If pytest ran but zero passed/failed tests (all skipped), treat as failure.
    """
    results = []

    for cmd in test_commands:
        try:
            result = run(
                cmd,
                cwd=worktree,
                timeout=timeout,
                cancel_check=cancel_check,
                max_output_bytes=max_output_bytes,
            )
        except FileNotFoundError as exc:
            raise ToolMissingError(
                "Validation tool not executable: "
                "%s (%s)" % (cmd[0], exc)
            ) from exc

        evidence.append(result)
        results.append(result)

    passed = all(
        item["returncode"] == 0 and not item["timed_out"]
        for item in results
    ) if results else True

    # Additional guard: if any command used pytest and it saw zero
    # passed/failed tests, treat as failure (all-skipped suite).
    if passed:
        for i, cmd in enumerate(test_commands):
            if isinstance(cmd, (list, tuple)):
                cmd_str = " ".join(cmd)
            else:
                cmd_str = str(cmd)
            if "pytest" in cmd_str:
                out = results[i].get("stdout", "") + results[i].get("stderr", "")
                if _pytest_zero_actual_tests(out):
                    passed = False
                    # Add evidence that the test suite had zero actual tests
                    evidence.append(
                        {
                            "type": "validation_zero_actual_tests",
                            "cmd": cmd_str,
                            "timestamp": now_iso(),
                        }
                    )
                    break

    return passed, results


def interpret_result(result: dict) -> str:
    """Classify a single run result for failure interpretation."""
    if result.get("cancelled"):
        return "cancelled"
    if result.get("timed_out"):
        return "timeout"
    if result.get("returncode") == 0:
        return "passed"
    return "failed"


def interpret_results(results: list) -> dict:
    """Reduce run results to an explicit per-check interpretation."""
    return {
        "checks": [
            {
                "cmd": result.get("cmd"),
                "returncode": result.get("returncode"),
                "timed_out": bool(result.get("timed_out")),
                "cancelled": bool(result.get("cancelled")),
                "interpretation": interpret_result(result),
            }
            for result in results
        ],
        "status": "PASS"
        if all(
            interpret_result(result) == "passed" for result in results
        )
        else "FAIL",
    }


def build_failure_context(
    attempt: int,
    test_results: list,
    diff_text: str,
    touched_files: list,
) -> dict:
    """Package failure state for the next corrective planning call."""
    return {
        "attempt": attempt,
        "tests": test_results,
        "diff": diff_text,
        "touched_files": touched_files,
    }