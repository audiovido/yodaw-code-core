#!/usr/bin/env python3.12
"""
YODAW Bootstrap Script

Handles installation, dependency detection, and first-run setup for YODAW.
Creates a self-contained installation with isolated Python environment.
"""
import os
import sys
import subprocess
import shutil
import venv
import argparse
import json
import platform
import socket
from pathlib import Path

def check_python_version():
    """Check that Python 3.12 is available."""
    try:
        output = subprocess.check_output(["python3.12", "--version"], text=True)
        if not output.startswith("Python 3.12"):
            print(f"Error: Python 3.12 required, found: {output.strip()}")
            return False
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: Python 3.12 not found. Please install Python 3.12.")
        return False
    return True

def check_git():
    """Check that git is available."""
    try:
        subprocess.check_output(["git", "--version"], text=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Warning: git not found. Version information will be limited.")
        return False

def get_system_info():
    """Get system information for dependency detection."""
    info = {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "architecture": platform.machine(),
        "processor": platform.processor(),
    }
    
    # macOS specific checks
    if info["platform"] == "Darwin":
        try:
            # Check if Apple Silicon
            output = subprocess.check_output(["uname", "-m"], text=True).strip()
            info["is_apple_silicon"] = output == "arm64"
            info["is_intel"] = output == "x86_64"
        except subprocess.CalledProcessError:
            info["is_apple_silicon"] = False
            info["is_intel"] = False
            
        # Get macOS version
        try:
            output = subprocess.check_output(["sw_vers", "-productVersion"], text=True).strip()
            info["macos_version"] = output
        except subprocess.CalledProcessError:
            info["macos_version"] = "unknown"
    
    return info

def check_writable_directory(path):
    """Check if a directory is writable."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        test_file.touch()
        test_file.unlink()
        return True
    except (OSError, PermissionError):
        return False

def check_port_available(host, port):
    """Check if a port is available for binding."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.bind((host, port))
            return True
    except OSError:
        return False

def get_commit_sha(source_dir):
    """Get the commit SHA from the source directory."""
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=source_dir,
            text=True
        ).strip()
        return output
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"

def get_branch_name(source_dir):
    """Get the current branch name."""
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=source_dir,
            text=True
        ).strip()
        return output
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"

def create_directories(install_dir):
    """Create the installation directory structure."""
    dirs = [
        install_dir / "bin",
        install_dir / "lib",
        install_dir / "var"  # For data, logs, etc.
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs

def copy_source(source_dir, dest_dir, exclude_patterns=None):
    """Copy source code to destination directory, excluding unnecessary files."""
    if exclude_patterns is None:
        exclude_patterns = {"dirs": set(), "files": set()}
    
    # Default exclusions
    default_exclude_dirs = {".git", ".github", "workspace", "data", "__pycache__", "*.pyc", "*.pyo", ".mypy_cache", ".pytest_cache", ".coverage", "htmlcov", "dist", "build", "*.egg-info", "output", "logs"}
    default_exclude_files = {".DS_Store", ".gitignore", ".gitmodules", "*.pyc", "*.pyo"}
    
    exclude_dirs = default_exclude_dirs.union(exclude_patterns.get("dirs", set()))
    exclude_files = default_exclude_files.union(exclude_patterns.get("files", set()))
    
    # Create destination directory for yodaw specifically
    yodaw_dest = dest_dir / "yodaw"
    if yodaw_dest.exists():
        shutil.rmtree(yodaw_dest)
    yodaw_dest.mkdir(parents=True)
    
    # Copy files to the yodaw subdirectory
    for item in source_dir.rglob("*"):
        # Skip if in excluded directory
        if any(excl in item.parts for excl in exclude_dirs):
            continue
        if item.is_file() and item.name in exclude_files:
            continue
        
        # Skip if item is inside the yodaw destination (to avoid infinite recursion)
        try:
            item.relative_to(yodaw_dest)
            continue  # Skip items that are inside the destination
        except ValueError:
            pass  # Not inside dest_dir, continue
        
        # Compute relative path from source
        try:
            rel_path = item.relative_to(source_dir)
        except ValueError:
            # Should not happen, but skip if it does
            continue
            
        target = yodaw_dest / rel_path
        
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)

def create_virtualenv(lib_dir):
    """Create a virtual environment in the lib directory."""
    venv_path = lib_dir / ".venv"
    venv.create(venv_path, with_pip=True)
    return venv_path

def install_dependencies(venv_path, lib_dir):
    """Install Python dependencies from requirements.txt."""
    pip_path = venv_path / "bin" / "pip"
    requirements_path = lib_dir / "yodaw" / "requirements.txt"
    
    if not requirements_path.exists():
        print(f"Warning: requirements.txt not found at {requirements_path}")
        return False
    
    try:
        subprocess.check_call([
            str(pip_path), "install", "-r", str(requirements_path)
        ])
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error installing dependencies: {e}")
        return False

def create_version_file(lib_dir, commit_sha, branch_name, system_info):
    """Create a version file in the installed package."""
    version_file = lib_dir / "yodaw" / "version.py"
    version_file.parent.mkdir(parents=True, exist_ok=True)
    with open(version_file, "w") as f:
        f.write(f'''"""
YODAW version information.
"""
__version__ = "0.1.0"
__commit__ = "{commit_sha}"
__branch__ = "{branch_name}"
__build_platform__ = "{system_info.get('platform', 'unknown')}"
__build_arch__ = "{system_info.get('architecture', 'unknown')}"
__build_macos_version__ = "{system_info.get('macos_version', 'unknown')}"
__build_timestamp__ = "{subprocess.check_output(['date', '-u', '+%Y-%m-%dT%H:%M:%SZ'], text=True).strip()}"
''')

def create_wrapper_script(bin_dir, lib_dir):
    """Create a simple wrapper script to run YODAW runtime."""
    wrapper_path = bin_dir / "yodaw"
    # Determine the Python interpreter from the virtual environment
    python_path = lib_dir / ".venv" / "bin" / "python"
    
    wrapper_content = '''#!/usr/bin/env bash
# YODAW wrapper script
# Sets up the environment and runs YODAW runtime, with lightweight
# version/status subcommands that need no server.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
VENV_PYTHON="$LIB_DIR/.venv/bin/python"
APP_DIR="$LIB_DIR/yodaw"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "Error: YODAW virtual environment not found. Please reinstall." >&2
    exit 1
fi

cd "$APP_DIR" || {
    echo "Error: Could not change to YODAW library directory" >&2
    exit 1
}

case "${1:-run}" in
    version)
        "$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '.')
from app.product.version import describe
import json
print(json.dumps(describe(), indent=2))
" 2>/dev/null || "$VENV_PYTHON" -c "
import sys
sys.path.insert(0, '.')
try:
    from app.product.version import describe
except ImportError:
    import json
    try:
        from version import __version__, __commit__, __branch__
        print(json.dumps({'product_version': __version__, 'commit': __commit__, 'branch': __branch__}, indent=2))
    except ImportError:
        print(json.dumps({'product_version': 'unknown', 'commit': 'unknown'}, indent=2))
"
        ;;
    status)
        "$VENV_PYTHON" -c "
import sys, json, os
sys.path.insert(0, '.')
try:
    from app.product.version import describe
    doc = describe()
except ImportError:
    doc = {'product_version': 'unknown', 'commit': 'unknown'}
doc['installed_at'] = os.environ.get('YODAW_INSTALL_DIR', '$SCRIPT_DIR/..')
print(json.dumps(doc, indent=2))
"
        ;;
    health)
        port="${YODAW_PORT:-8844}"
        host="${YODAW_HOST:-127.0.0.1}"
        curl -s "http://$host:$port/api/v1/health" || { echo "YODAW: unhealthy (unreachable)" >&2; exit 1; }
        ;;
    run|serve|start)
        shift
        exec "$VENV_PYTHON" -m app.runtime "$@"
        ;;
    *)
        exec "$VENV_PYTHON" -m app.runtime "$@"
        ;;
esac
'''
    with open(wrapper_path, "w") as f:
        f.write(wrapper_content)
    wrapper_path.chmod(0o755)  # Make executable

def create_uninstall_script(bin_dir, install_dir):
    """Create an uninstall script."""
    uninstall_path = bin_dir / "yodaw-uninstall"
    uninstall_content = f'''#!/usr/bin/env bash
# YODAW uninstall script

INSTALL_DIR="{install_dir}"

if [ ! -d "$INSTALL_DIR" ]; then
    echo "Error: YODAW installation not found at $INSTALL_DIR"
    exit 1
fi

echo "This will remove the YODAW installation at:"
echo "  $INSTALL_DIR"
echo ""
read -p "Are you sure you want to uninstall YODAW? [y/N] " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Uninstall cancelled."
    exit 1
fi

echo "Removing YODAW installation..."
rm -rf "$INSTALL_DIR"
echo "YODAW has been uninstalled."
'''
    with open(uninstall_path, "w") as f:
        f.write(uninstall_content)
    uninstall_path.chmod(0o755)

def create_release_artifact(source_dir, version_info, output_dir):
    """Create a release tarball."""
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a temporary directory for the release contents
    release_name = f"yodaw-{version_info['version']}"
    temp_dir = output_dir / release_name
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    
    # Define exclusion patterns for release
    exclude_patterns = {
        "dirs": {".git", ".github", "workspace", "data", "__pycache__", "*.pyc", "*.pyo", ".mypy_cache", ".pytest_cache", ".coverage", "htmlcov", "dist", "build", "*.egg-info", "output", "logs"},
        "files": {".DS_Store", ".gitignore", ".gitmodules", "*.pyc", "*.pyo"}
    }
    
    # Copy source (excluding unwanted files)
    copy_source(source_dir, temp_dir, exclude_patterns)
    
    # Create tarball
    tarball_path = output_dir / f"{release_name}.tar.gz"
    shutil.make_archive(
        str(tarball_path).replace(".tar.gz", ""),
        "gztar",
        root_dir=temp_dir
    )
    
    # Clean up temp directory
    shutil.rmtree(temp_dir)
    
    return tarball_path

def perform_dependency_checks(install_dir):
    """Perform comprehensive dependency and environment checks."""
    system_info = get_system_info()
    
    checks = {
        "python_312": check_python_version(),
        "git": check_git(),
        "platform": system_info.get("platform") == "Darwin",
        "macos_version": system_info.get("macos_version", "unknown") != "unknown",
        "architecture": system_info.get("architecture") in ["x86_64", "arm64"],
        "writable_bin": check_writable_directory(install_dir / "bin"),
        "writable_lib": check_writable_directory(install_dir / "lib"),
        "writable_var": check_writable_directory(install_dir / "var"),
    }
    
    # Default ports to check
    default_host = "127.0.0.1"
    default_port = 8844
    checks["port_available"] = check_port_available(default_host, default_port)
    
    return checks, system_info

def main():
    parser = argparse.ArgumentParser(description="Bootstrap YODAW installation")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path.cwd(),
        help="Path to YODAW source directory (default: current directory)"
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=Path.home() / ".yodaw",
        help="Installation directory (default: ~/.yodaw)"
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Skip dependency installation"
    )
    parser.add_argument(
        "--create-release",
        action="store_true",
        help="Create release artifact instead of installing"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path.cwd() / "dist",
        help="Output directory for release artifacts (default: ./dist)"
    )
    parser.add_argument(
        "--check-deps-only",
        action="store_true",
        help="Only perform dependency checks, don't install"
    )
    args = parser.parse_args()
    
    # Validate source directory
    if not args.source.exists():
        print(f"Error: Source directory does not exist: {args.source}")
        sys.exit(1)
    
    if args.check_deps_only:
        print("Performing dependency checks only...")
        checks, system_info = perform_dependency_checks(args.install_dir)
        
        print("\nDependency Check Results:")
        print("=" * 50)
        for check_name, result in checks.items():
            status = "PASS" if result else "FAIL"
            print(f"{check_name:.<30} {status}")
        
        print("\nSystem Information:")
        print("=" * 50)
        for key, value in system_info.items():
            print(f"{key}: {value}")
        
        # Exit with error code if any critical checks failed
        critical_checks = ["python_312", "writable_bin", "writable_lib", "writable_var", "port_available"]
        failed_critical = [check for check in critical_checks if not checks.get(check, True)]
        
        if failed_critical:
            print(f"\nCritical checks failed: {', '.join(failed_critical)}")
            sys.exit(1)
        else:
            print("\nAll critical checks passed!")
            return
    
    if args.create_release:
        print("Creating release artifact...")
        version_info = {
            "version": "0.1.0",
            "commit": get_commit_sha(args.source),
            "branch": get_branch_name(args.source),
        }
        system_info = get_system_info()
        
        tarball = create_release_artifact(args.source, version_info, args.output_dir)
        print(f"Release artifact created: {tarball}")
        
        # Also create version info file
        version_file = args.output_dir / "VERSION"
        with open(version_file, "w") as f:
            f.write(f"{version_info['version']}\n")
            f.write(f"commit: {version_info['commit']}\n")
            f.write(f"branch: {version_info['branch']}\n")
            f.write(f"platform: {system_info.get('platform', 'unknown')}\n")
            f.write(f"architecture: {system_info.get('architecture', 'unknown')}\n")
        
        print(f"Version info: {version_file}")
        return
    
    print(f"Bootstrapping YODAW from: {args.source}")
    print(f"Installation directory: {args.install_dir}")
    
    # Perform dependency checks
    print("Performing dependency checks...")
    checks, system_info = perform_dependency_checks(args.install_dir)
    
    print("\nDependency Check Results:")
    print("=" * 50)
    for check_name, result in checks.items():
        status = "PASS" if result else "FAIL"
        print(f"{check_name:.<30} {status}")
    
    print("\nSystem Information:")
    print("=" * 50)
    for key, value in system_info.items():
        print(f"{key}: {value}")
    
    # Check critical prerequisites
    critical_checks = ["python_312", "writable_bin", "writable_lib", "writable_var", "port_available"]
    failed_critical = [check for check in critical_checks if not checks.get(check, True)]
    
    if failed_critical:
        print(f"\nCritical checks failed: {', '.join(failed_critical)}")
        print("Cannot proceed with installation.")
        sys.exit(1)
    
    # Check Python version (redundant with above but clear)
    if not check_python_version():
        sys.exit(1)
    check_git()  # Warning only
    
    # Create directories
    bin_dir, lib_dir, var_dir = create_directories(args.install_dir)
    print(f"Created directories: {bin_dir}, {lib_dir}, {var_dir}")
    
    # Copy source code
    print("Copying source code...")
    copy_source(args.source, lib_dir)
    
    # Get commit SHA and branch for version information
    commit_sha = get_commit_sha(args.source)
    branch_name = get_branch_name(args.source)
    print(f"Commit SHA: {commit_sha}")
    print(f"Branch: {branch_name}")
    
    # Create virtual environment
    print("Creating virtual environment...")
    venv_path = create_virtualenv(lib_dir)
    
    # Install dependencies
    if not args.skip_deps:
        print("Installing dependencies...")
        if not install_dependencies(venv_path, lib_dir):
            print("Error: Failed to install dependencies")
            sys.exit(1)
    else:
        print("Skipping dependency installation")
    
    # Create version file
    create_version_file(lib_dir, commit_sha, branch_name, system_info)
    print("Created version file")
    
    # Create wrapper script
    create_wrapper_script(bin_dir, lib_dir)
    print(f"Created wrapper script: {bin_dir / 'yodaw'}")
    
    # Create uninstall script
    create_uninstall_script(bin_dir, args.install_dir)
    print(f"Created uninstall script: {bin_dir / 'yodaw-uninstall'}")
    
    print("\nBootstrap complete!")
    print(f"To run YODAW: {bin_dir / 'yodaw'}")
    print(f"To uninstall: {bin_dir / 'yodaw-uninstall'}")
    print(f"Data directory: {var_dir}")
    print(f"Version: 0.1.0 (commit {commit_sha[:8]})")
    
    # Final verification
    version_script = lib_dir / "yodaw" / "version.py"
    if version_script.exists():
        print(f"Version info available at: {version_script}")

if __name__ == "__main__":
    main()