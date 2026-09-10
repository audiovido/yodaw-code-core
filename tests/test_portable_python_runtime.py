"""Portable interpreter resolution never depends on bare `python`."""
import sys

from app.workers.python_runtime import resolve_python_executable


def test_prefers_running_interpreter():
    assert resolve_python_executable() == sys.executable


def test_configured_override_when_no_running_interpreter(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setenv("YODAW_PYTHON", "/custom/python")
    assert resolve_python_executable() == "/custom/python"


def test_falls_back_to_python3_on_path(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.delenv("YODAW_PYTHON", raising=False)
    monkeypatch.setattr(
        "app.workers.python_runtime.shutil.which",
        lambda name: "/usr/bin/python3" if name == "python3" else None,
    )
    assert resolve_python_executable() == "/usr/bin/python3"


def test_falls_back_to_python_on_path(monkeypatch):
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.delenv("YODAW_PYTHON", raising=False)
    monkeypatch.setattr(
        "app.workers.python_runtime.shutil.which",
        lambda name: "/usr/bin/python" if name == "python" else None,
    )
    assert resolve_python_executable() == "/usr/bin/python"
