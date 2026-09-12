"""
YODAW Product Version Information

Provides runtime-accessible version and build metadata.
"""

from __future__ import annotations

import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


def _read_version_file() -> Dict[str, Any]:
    """Read version information from the version.py file."""
    version_info = {}
    try:
        version_path = Path(__file__).parent / "version.py"
        if version_path.exists():
            # Execute the version file to extract variables
            namespace: Dict[str, Any] = {}
            with open(version_path, "r") as f:
                code = f.read()
            # Execute in the namespace - we trust our own version file
            exec(code, namespace)
            version_info.update(
                {
                    k: v
                    for k, v in namespace.items()
                    if k
                    in {
                        "__version__",
                        "__commit__",
                        "__branch__",
                        "__build_platform__",
                        "__build_arch__",
                        "__build_macos_version__",
                        "__build_timestamp__",
                    }
                }
            )
    except Exception:
        pass  # Ignore errors reading version file
    return version_info


def _get_git_info(source_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Get git commit and branch information if available."""
    info = {}
    if source_dir is None:
        source_dir = Path(__file__).resolve().parents[2]  # Go up to repo root

    try:
        # Get commit SHA
        commit_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if commit_result.returncode == 0:
            info["commit"] = commit_result.stdout.strip()

        # Get branch name
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if branch_result.returncode == 0:
            info["branch"] = branch_result.stdout.strip()

        # Get dirty status
        dirty_result = subprocess.run(
            ["git", "diff", "--quiet"],
            cwd=source_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        info["dirty"] = dirty_result.returncode != 0
    except Exception:
        pass  # Ignore git errors

    return info


def describe(source_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Return a dictionary containing version and build information.

    Args:
        source_dir: Optional path to source directory for git info.
                   If not provided, attempts to detect from __file__.

    Returns:
        Dictionary with version metadata.
    """
    if source_dir is None:
        source_dir = Path(__file__).resolve().parents[2]

    # Start with git information
    version_info = _get_git_info(source_dir)

    # Add/overwrite with version file information (takes precedence for installed copies)
    version_info.update(_read_version_file())

    # Add runtime information
    version_info.update(
        {
            "product_version": version_info.get("__version__", "0.1.0"),
            "runtime_platform": platform.system(),
            "runtime_platform_release": platform.release(),
            "runtime_platform_version": platform.version(),
            "runtime_architecture": platform.machine(),
            "runtime_processor": platform.processor(),
            "python_version": platform.python_version(),
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
    )

    # Normalize keys for external consumption
    result = {
        "product_version": version_info.get("product_version", "unknown"),
        "commit": version_info.get("__commit__", version_info.get("commit", "unknown")),
        "branch": version_info.get("__branch__", version_info.get("branch", "unknown")),
        "dirty": version_info.get("dirty", False),
        "build": {
            "platform": version_info.get(
                "__build_platform__", version_info.get("build_platform", platform.system())
            ),
            "architecture": version_info.get(
                "__build_arch__", version_info.get("build_arch", platform.machine())
            ),
            "macos_version": version_info.get(
                "__build_macos_version__", version_info.get("build_macos_version", "unknown")
            ),
            "timestamp": version_info.get(
                "__build_timestamp__", version_info.get("build_timestamp", "unknown")
            ),
        },
        "runtime": {
            "platform": version_info.get("runtime_platform", platform.system()),
            "platform_release": version_info.get(
                "runtime_platform_release", platform.release()
            ),
            "platform_version": version_info.get(
                "runtime_platform_version", platform.version()
            ),
            "architecture": version_info.get(
                "runtime_architecture", platform.machine()
            ),
            "processor": version_info.get(
                "runtime_processor", platform.processor()
            ),
            "python_version": version_info.get("python_version", platform.python_version()),
        },
        "timestamp_utc": version_info.get(
            "timestamp_utc", datetime.now(timezone.utc).isoformat()
        ),
    }

    return result


def to_json() -> str:
    """Return version information as a JSON string."""
    return json.dumps(describe(), indent=2, sort_keys=True)


if __name__ == "__main__":
    # When run directly, print JSON to stdout
    print(to_json())