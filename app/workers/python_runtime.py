"""Portable Python interpreter resolution for worker subprocesses.

Order: running interpreter (sys.executable), configured override
(YODAW_PYTHON), python3 on PATH, then python on PATH.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Optional


def resolve_python_executable(configured: str | None = None) -> str:
    if sys.executable:
        return sys.executable
    override = configured or os.environ.get("YODAW_PYTHON")
    if override:
        return override
    found = shutil.which("python3") or shutil.which("python")
    return found or "python3"


def _discover_user_site_base(paths, prefix) -> Optional[str]:
    """Find the boot-time user site-packages base in ``paths``.

    A user-site interpreter (e.g. macOS Command Line Tools python)
    resolves its packages directory from $HOME at boot. When kodgar
    later runs with a relocated HOME (daemons, launchd, hermetic
    tests), subprocesses spawned by workers can no longer import that
    interpreter's own packages. Repinning PYTHONUSERBASE to the base
    discovered at boot keeps worker validation environments identical
    to the environment kodgar itself booted with.

    Returns None for stdlib-only interpreters and virtualenvs (whose
    site-packages are absolute-path pinned and HOME-independent), so
    the returned env dict is a no-op there.
    """
    for path in paths:
        if not path or os.path.basename(path) != "site-packages":
            continue
        marker = "/lib/python"
        if marker not in path:
            continue
        base = path.split(marker, 1)[0]
        if not base or not os.path.isdir(path):
            continue
        if os.path.exists(os.path.join(base, "pyvenv.cfg")):
            continue  # virtualenv: HOME-independent, no repin needed
        if base == prefix:
            continue
        return base
    return None


# Captured once at import, i.e. under whatever HOME kodgar booted with.
_BOOT_USER_SITE_BASE = _discover_user_site_base(sys.path, sys.prefix)


def user_site_env(base: Optional[str] = None) -> dict:
    """Env additions repinning the boot-time user site-packages.

    An explicit PYTHONUSERBASE set by the operator always wins.
    """
    if os.environ.get("PYTHONUSERBASE"):
        return {}
    if base is None:
        base = _BOOT_USER_SITE_BASE
    if not base:
        return {}
    return {"PYTHONUSERBASE": base}
