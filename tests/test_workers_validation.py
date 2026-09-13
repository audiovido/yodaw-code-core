"""Focused tests for validation: language-aware detection, tool-missing
fail-fast, no-test semantics, and timeout/failure interpretation."""

import sys

import pytest

from app.workers.validation import (
    build_failure_context,
    detect_test_commands,
    interpret_result,
    interpret_results,
    run_validation,
)
from app.workers.worker_errors import ToolMissingError

PYTHON = sys.executable


def _no_tool(_name):
    return None


def _has_tool(_name):
    return "/usr/bin/tool"


def test_detect_python(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    commands = detect_test_commands(tmp_path, python_executable=PYTHON)
    assert any("-m" in cmd and "pytest" in cmd for cmd in commands)


def test_detect_jsts(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}\n')
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["npm", "test", "--", "--runInBand"] in commands


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["go", "test", "./..."] in commands


def test_detect_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["cargo", "test", "--quiet"] in commands


def test_detect_java_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["mvn", "-q", "test"] in commands


def test_detect_java_gradle(tmp_path):
    (tmp_path / "build.gradle").write_text("apply plugin: 'java'\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["gradle", "-q", "test"] in commands


def test_detect_c_cpp_ctest(tmp_path):
    (tmp_path / "CMakeLists.txt").write_text("project(x)\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["ctest", "--test-dir", "."] in commands


def test_detect_c_cpp_make(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\techo ok\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["make", "test"] in commands


def test_detect_csharp(tmp_path):
    (tmp_path / "App.csproj").write_text("<Project/>\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["dotnet", "test"] in commands


def test_detect_swift(tmp_path):
    (tmp_path / "Package.swift").write_text("// swift-tools-version:5.9\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert ["swift", "test"] in commands


def test_detect_bash(tmp_path):
    (tmp_path / "script.sh").write_text("echo hi\n")
    commands = detect_test_commands(tmp_path, tool_check=_has_tool)
    assert any(cmd[1:3] == ["-n", str(tmp_path / "script.sh")] for cmd in commands)


def test_detect_sql(tmp_path):
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (id INTEGER);\n")
    commands = detect_test_commands(tmp_path, python_executable=PYTHON)
    assert any(
        "-c" in cmd and any("sqlite3" in str(part) for part in cmd)
        for cmd in commands
    )


def test_detect_no_tests(tmp_path):
    assert detect_test_commands(tmp_path) == []


def test_tool_missing_fails_fast(tmp_path):
    (tmp_path / "package.json").write_text("{}\n")
    with pytest.raises(ToolMissingError) as excinfo:
        detect_test_commands(tmp_path, tool_check=_no_tool)
    assert "npm" in str(excinfo.value)


def test_tool_missing_java(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>\n")
    with pytest.raises(ToolMissingError):
        detect_test_commands(tmp_path, tool_check=_no_tool)


def test_no_tests_passes_vacuously(tmp_path):
    passed, results = run_validation(tmp_path, [], [], timeout=5)
    assert passed is True
    assert results == []


def test_validation_passes(tmp_path):
    passed, results = run_validation(
        tmp_path,
        [],
        [[PYTHON, "-c", "pass"]],
        timeout=10,
    )
    assert passed is True
    assert results[0]["returncode"] == 0


def test_validation_failure_interprets_returncode(tmp_path):
    passed, results = run_validation(
        tmp_path,
        [],
        [[PYTHON, "-c", "import sys; sys.exit(1)"]],
        timeout=10,
    )
    assert passed is False
    assert interpret_result(results[0]) == "failed"


def test_validation_timeout_interprets_as_failure(tmp_path):
    passed, results = run_validation(
        tmp_path,
        [],
        [[PYTHON, "-c", "import time; time.sleep(60)"]],
        timeout=0.4,
    )
    assert passed is False
    assert results[0]["timed_out"] is True
    assert interpret_result(results[0]) == "timeout"


def test_validation_missing_tool_raises(tmp_path):
    with pytest.raises(ToolMissingError):
        run_validation(
            tmp_path,
            [],
            [["/definitely/not/a/tool"]],
            timeout=10,
        )


def test_interpret_results_passes_and_fails():
    good = {"cmd": "t", "returncode": 0, "timed_out": False, "cancelled": False}
    bad = {"cmd": "t", "returncode": 2, "timed_out": False, "cancelled": False}
    timed = {"cmd": "t", "returncode": -9, "timed_out": True, "cancelled": False}

    assert interpret_results([good])["status"] == "PASS"
    assert interpret_results([good, bad])["status"] == "FAIL"
    assert interpret_results([timed])["checks"][0]["interpretation"] == "timeout"


def test_build_failure_context_shape():
    context = build_failure_context(
        attempt=1,
        test_results=[],
        diff_text="diff",
        touched_files=["a.py"],
    )
    assert context["attempt"] == 1
    assert context["tests"] == []
    assert context["diff"] == "diff"
    assert context["touched_files"] == ["a.py"]