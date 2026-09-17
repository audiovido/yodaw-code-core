"""Portable interpreter resolution never depends on bare `python`."""
import sys

from app.workers.python_runtime import (
    _discover_user_site_base,
    resolve_python_executable,
    user_site_env,
)


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


# ------------------------------------------------------- user-site env


def test_discover_finds_real_user_site_base(tmp_path):
    site_dir = tmp_path / "lib" / "python3.9" / "site-packages"
    site_dir.mkdir(parents=True)
    base = _discover_user_site_base([str(site_dir)], "/usr/local")
    assert base == str(tmp_path)


def test_discover_ignores_venv_and_prefix_and_missing():
    # virtualenv: pyvenv.cfg marks it HOME-independent
    venv_site = "/tmp/venv/lib/python3.12/site-packages"
    assert _discover_user_site_base([venv_site], "/usr/local") is None
    # inside sys.prefix (framework stdlib layout) -> not a user site
    assert _discover_user_site_base(
        ["/Library/Frameworks/lib/python3.9/site-packages"],
        "/Library/Frameworks",
    ) is None
    # nonexistent directory -> not usable
    assert (
        _discover_user_site_base(
            ["/nonexistent/lib/python3.9/site-packages"], "/usr"
        )
        is None
    )


def test_user_site_env_pins_boot_base(monkeypatch):
    monkeypatch.delenv("PYTHONUSERBASE", raising=False)
    env = user_site_env("/Users/dev")
    assert env == {"PYTHONUSERBASE": "/Users/dev"}


def test_user_site_env_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("PYTHONUSERBASE", "/opt/pyuser")
    assert user_site_env("/Users/dev") == {}


def test_user_site_env_noop_without_base(monkeypatch):
    monkeypatch.delenv("PYTHONUSERBASE", raising=False)
    import app.workers.python_runtime as pr

    monkeypatch.setattr(pr, "_BOOT_USER_SITE_BASE", None)
    assert user_site_env(None) == {}
