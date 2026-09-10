"""Portable Python interpreter resolution for worker subprocesses.

Order: running interpreter (sys.executable), configured override
(YODAW_PYTHON), python3 on PATH, then python on PATH.
"""
from __future__ import annotations

import os
import shutil
import sys


def resolve_python_executable(configured: str | None = None) -> str:
    if sys.executable:
        return sys.executable
    override = configured or os.environ.get("YODAW_PYTHON")
    if override:
        return override
    found = shutil.which("python3") or shutil.which("python")
    return found or "python3"
