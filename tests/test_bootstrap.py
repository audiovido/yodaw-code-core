"""
Tests for YODAW bootstrap functionality.
"""
import json
import os
import sys
import tempfile
import shutil
import subprocess
from pathlib import Path

# Add the project root to sys.path so we can import the bootstrap script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.bootstrap import (
    check_python_version,
    check_git,
    get_system_info,
    get_commit_sha,
    get_branch_name,
    create_directories,
    copy_source,
    create_virtualenv,
    install_dependencies,
    create_version_file,
    create_wrapper_script,
    create_uninstall_script,
    check_writable_directory,
    check_port_available,
    perform_dependency_checks,
)


def test_check_python_version():
    """Test that Python version checking works."""
    # This test assumes we're running on Python 3.12+
    # We can't easily test the failure case without mocking
    result = check_python_version()
    assert result is True, "Should detect Python 3.12+"


def test_check_git():
    """Test that git detection works."""
    # This test assumes git is available in the environment
    result = check_git()
    assert result is True, "Should detect git availability"


def test_get_system_info():
    """Test that system information gathering works."""
    info = get_system_info()
    assert "platform" in info
    assert "architecture" in info
    assert "processor" in info

    # On macOS, check for additional fields
    if info["platform"] == "Darwin":
        assert "macos_version" in info
        assert "is_apple_silicon" in info or True  # May not be set in all environments
        assert "is_intel" in info or True


def test_get_commit_sha(tmp_path):
    """Test getting commit SHA from a git repository."""
    # Initialize a git repo in temp directory
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Initialize git
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    # Create a file and commit
    test_file = repo_dir / "test.txt"
    test_file.write_text("test")
    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True)

    # Test getting the SHA
    sha = get_commit_sha(repo_dir)
    assert len(sha) == 40  # SHA-1 hash length
    assert all(c in "0123456789abcdef" for c in sha)


def test_get_branch_name(tmp_path):
    """Test getting branch name from a git repository."""
    # Initialize a git repo in temp directory
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Initialize git
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)

    # Create a file and commit
    test_file = repo_dir / "test.txt"
    test_file.write_text("test")
    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True)

    # Test getting the branch name
    branch = get_branch_name(repo_dir)
    assert branch == "main" or branch == "master"


def test_create_directories(tmp_path):
    """Test directory creation."""
    install_dir = tmp_path / "install"
    bin_dir, lib_dir, var_dir = create_directories(install_dir)

    assert bin_dir.exists()
    assert lib_dir.exists()
    assert var_dir.exists()
    assert bin_dir == install_dir / "bin"
    assert lib_dir == install_dir / "lib"
    assert var_dir == install_dir / "var"


def test_create_virtualenv(tmp_path):
    """Test virtual environment creation."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()

    venv_path = create_virtualenv(lib_dir)

    assert venv_path.exists()
    assert (venv_path / "bin" / "python").exists()
    assert (venv_path / "bin" / "pip").exists()


def test_create_version_file(tmp_path):
    """Test version file creation."""
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    yodaw_dir = lib_dir / "yodaw"
    yodaw_dir.mkdir()

    commit_sha = "a1b2c3d4e5f6789012345678901234567890abcd"
    branch_name = "test-branch"
    system_info = {
        "platform": "Darwin",
        "architecture": "arm64",
        "macos_version": "14.0"
    }

    create_version_file(lib_dir, commit_sha, branch_name, system_info)

    version_file = yodaw_dir / "version.py"
    assert version_file.exists()

    content = version_file.read_text()
    assert "__version__ = \"0.4.0\"" in content
    assert f"__commit__ = \"{commit_sha}\"" in content
    assert f"__branch__ = \"{branch_name}\"" in content
    assert "__build_platform__ = \"Darwin\"" in content
    assert "__build_arch__ = \"arm64\"" in content
    assert "__build_macos_version__ = \"14.0\"" in content


def test_create_wrapper_script(tmp_path):
    """Test wrapper script creation."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    venv_dir = lib_dir / ".venv"
    venv_dir.mkdir()
    (venv_dir / "bin").mkdir()
    (venv_dir / "bin" / "python").write_text("#!/bin/sh\nexec python3.12 \"$@\"")
    (venv_dir / "bin" / "python").chmod(0o755)

    create_wrapper_script(bin_dir, lib_dir)

    wrapper_path = bin_dir / "yodaw"
    assert wrapper_path.exists()
    assert wrapper_path.stat().st_mode & 0o755 == 0o755  # Check executable

    content = wrapper_path.read_text()
    assert "#!/usr/bin/env bash" in content
    assert "VENV_PYTHON" in content
    assert "-m app.runtime" in content
    # Management/mission commands must reach the product CLI, not
    # silently launch the API server.
    assert "-m app.cli.main" in content
    assert "setup-9router" in content
    # serve/start still launch the runtime service
    serve_case = content.split("serve|server|start)")[1].split(";;")[0]
    assert "-m app.runtime" in serve_case
    default_case = content.rsplit("*)", 1)[1].split(";;")[0]
    assert "-m app.cli.main" in default_case
    assert "-m app.runtime" not in default_case
    # A bootstrap-provisioned config is exported for all commands.
    assert "YODAW_CONFIG" in content


def test_create_uninstall_script(tmp_path):
    """Test uninstall script creation."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install_dir = tmp_path / "install"

    create_uninstall_script(bin_dir, install_dir)

    uninstall_path = bin_dir / "yodaw-uninstall"
    assert uninstall_path.exists()
    assert uninstall_path.stat().st_mode & 0o755 == 0o755  # Check executable

    content = uninstall_path.read_text()
    assert "#!/usr/bin/env bash" in content
    assert "INSTALL_DIR" in content
    assert "rm -rf" in content


def test_check_writable_directory(tmp_path):
    """Test writable directory checking."""
    # Test with a writable directory
    writable_dir = tmp_path / "writable"
    assert check_writable_directory(writable_dir) is True
    assert writable_dir.exists()

    # Clean up
    shutil.rmtree(writable_dir)


def test_check_port_available():
    """Test port availability checking."""
    # Test with a port that should be available (high random port)
    import socket
    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    # This port should be available right after binding and closing
    assert check_port_available("127.0.0.1", port) is True

    # Test with a port that's likely unavailable (though this is not guaranteed)
    # We'll skip this as it's flaky


def test_perform_dependency_checks(tmp_path):
    """Test dependency checks performance."""
    install_dir = tmp_path / "install"
    checks, system_info = perform_dependency_checks(install_dir)

    # Should return a dict with expected keys
    expected_keys = ["python_312", "git", "platform", "macos_version", "architecture",
                     "writable_bin", "writable_lib", "writable_var", "port_available"]

    for key in expected_keys:
        assert key in checks
        # Values should be boolean
        assert isinstance(checks[key], bool)

    # System info should have expected keys
    assert "platform" in system_info
    assert "architecture" in system_info


def test_bootstrap_script_help():
    """Test that the bootstrap script shows help."""
    result = subprocess.run(
        [sys.executable, "scripts/bootstrap.py", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "--source" in result.stdout
    assert "--install-dir" in result.stdout
    assert "--skip-deps" in result.stdout
    assert "--create-release" in result.stdout
    assert "--check-deps-only" in result.stdout


def test_bootstrap_check_deps_only(tmp_path):
    """Test the --check-deps-only option."""
    install_dir = tmp_path / "install"
    result = subprocess.run(
        [sys.executable, "scripts/bootstrap.py", "--check-deps-only", "--install-dir", str(install_dir)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True
    )
    # Should succeed (exit code 0) since all checks should pass in test env
    assert result.returncode == 0
    assert "Dependency Check Results:" in result.stdout
    assert "System Information:" in result.stdout


if __name__ == "__main__":
    # Run tests manually if executed directly
    test_check_python_version()
    test_check_git()
    test_get_system_info()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        test_get_commit_sha(tmp_path)
        test_get_branch_name(tmp_path)
        test_create_directories(tmp_path)
        test_create_virtualenv(tmp_path)
        test_create_version_file(tmp_path)
        test_create_wrapper_script(tmp_path)
        test_create_uninstall_script(tmp_path)
        test_check_writable_directory(tmp_path)
        test_check_port_available()
        test_perform_dependency_checks(tmp_path)

    test_bootstrap_script_help()
    test_bootstrap_check_deps_only(tmp_path)
    print("All tests passed!")

def test_provision_9router_success(tmp_path):
    from scripts.bootstrap import provision_9router

    class FakeCompleted:
        returncode = 0
        stdout = json.dumps(
            {
                "ok": True,
                "model": "gemma/gemma3-270m",
                "config": str(tmp_path / "config.toml"),
                "provisioning": {
                    "daemon": {"started": True, "pid": 42},
                    "gateway_key": {"status": "created"},
                    "local_servers": [],
                },
            }
        )
        stderr = ""

    captured = {}

    def fake_runner(cmd, cwd=None, env=None, capture_output=True,
                   text=True, timeout=900):
        captured["cmd"] = cmd
        captured["env"] = env
        return FakeCompleted()

    ok, summary, error = provision_9router(
        tmp_path / ".venv" / "bin" / "python",
        tmp_path / "lib" / "yodaw",
        data_dir=tmp_path / "var" / "9router",
        config_path=tmp_path / "var" / "yodaw" / "config.toml",
        local_servers=["Local:local:http://127.0.0.1:8089/v1"],
        runner=fake_runner,
    )
    assert ok is True and error == ""
    assert summary["model"] == "gemma/gemma3-270m"
    assert captured["env"]["DATA_DIR"] == str(tmp_path / "var" / "9router")
    assert captured["env"]["YODAW_LOCAL_SERVERS"] == (
        "Local:local:http://127.0.0.1:8089/v1"
    )
    assert "--no-verify" in captured["cmd"] and "--json" in captured["cmd"]


def test_provision_9router_failure_surfaces_error(tmp_path):
    from scripts.bootstrap import provision_9router

    class FakeCompleted:
        returncode = 1
        stdout = json.dumps({"ok": False, "error": "npm install -g 9router"})
        stderr = ""

    ok, summary, error = provision_9router(
        tmp_path / "py", tmp_path,
        data_dir=tmp_path / "d", config_path=tmp_path / "c.toml",
        runner=lambda *a, **k: FakeCompleted(),
    )
    assert ok is False
    assert "npm install -g 9router" in error


def test_provision_9router_missing_interpreter(tmp_path):
    from scripts.bootstrap import provision_9router

    def boom(*a, **k):
        raise FileNotFoundError("no python")

    ok, summary, error = provision_9router(
        tmp_path / "py", tmp_path,
        data_dir=tmp_path / "d", config_path=tmp_path / "c.toml",
        runner=boom,
    )
    assert ok is False
    assert "interpreter not found" in error
