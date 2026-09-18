"""Managed clean source for background execution.

Background tasks must never depend on the user's working copy being
clean — and must never "fix" it either (no clean, no reset, no stash,
no checkout *of the user's tree*). The contract:

- a **clean** user repo is used directly (as today);
- a **dirty** user repo is served from a *managed mirror*: a dedicated
  bare-ish clone under the managed root, refreshed with a read-only
  ``git fetch <user repo> HEAD`` and then reset — **inside the mirror,
  which is Kodgar-owned state**, never inside the user's tree.

The user's repo is only ever *read* (status, rev-parse, fetch source).
Uncommitted user changes are deliberately NOT copied: tasks build on
committed history, which is the only state Kodgar can verify and
reproduce.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Optional

from app.workers.safe_subprocess import run as run_command

MANAGED_ROOT_DEFAULT = "~/.kodgar/managed-source"

_lock = threading.Lock()


def managed_root() -> Path:
    from pathlib import os as _os

    root = Path(
        _os.environ.get("KODGAR_MANAGED_SOURCE_ROOT", MANAGED_ROOT_DEFAULT)
    ).expanduser()
    return root.resolve()


def _is_clean(repo: Path) -> bool:
    result = run_command(["git", "status", "--porcelain"], cwd=str(repo))
    return result.get("returncode") == 0 and not (result.get("stdout") or "").strip()


def _safe_dir_name(repo: Path) -> str:
    """Stable, filesystem-safe name for one source repo."""
    raw = str(repo.resolve())
    digest = hashlib.sha1(raw.encode()).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo.name).strip("-") or "repo"
    return f"{slug}-{digest}"


def resolve_execution_source(repo: str) -> dict[str, Any]:
    """Return ``{"repo": path, "mirror": bool, "error": ...}`` for a task.

    Clean repo -> used directly. Dirty repo -> a managed mirror under
    ``KODGAR_MANAGED_SOURCE_ROOT`` is created/refreshed from the user's
    HEAD and used instead. The user's tree is never mutated; when no
    mirror can be established the original DirtyRepo semantics are
    preserved as a truthful error.
    """
    source = Path(repo).expanduser().resolve()
    if _is_clean(source):
        return {"repo": str(source), "mirror": False, "error": None}

    root = managed_root() / _safe_dir_name(source)
    with _lock:
        return _mirror_from(source, root)


def _git(repo: Path, args: list[str], timeout: int = 120) -> dict:
    return run_command(["git", *args], cwd=str(repo), timeout=timeout)


def _mirror_from(source: Path, mirror_root: Path) -> dict[str, Any]:
    mirror: Optional[Path] = None
    try:
        if (mirror_root / ".git").exists():
            mirror = mirror_root
        else:
            mirror_root.mkdir(parents=True, exist_ok=True)
            created = run_command(
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    str(source),
                    str(mirror_root),
                ],
                timeout=300,
            )
            if created.get("returncode") != 0:
                return {
                    "repo": str(source),
                    "mirror": False,
                    "error": {
                        "type": "MirrorError",
                        "message": (
                            (created.get("stderr") or created.get("stdout") or "")[:400]
                        ),
                    },
                }
            mirror = mirror_root

        # Read-only refresh from the user's HEAD: fetch (does not touch
        # the user's tree), then re-point the mirror's own branch with
        # ``checkout -B``. We never fetch *into* refs/heads/mirror-source
        # directly: git refuses to update a ref that is currently
        # checked out, which would break every refresh after the first.
        fetched = _git(mirror, ["fetch", "--prune", str(source), "HEAD"], timeout=300)
        if fetched.get("returncode") != 0:
            return {
                "repo": str(source),
                "mirror": False,
                "error": {
                    "type": "MirrorError",
                    "message": (
                        (fetched.get("stderr") or fetched.get("stdout") or "")[:400]
                    ),
                },
            }
        checkout = _git(mirror, ["checkout", "-q", "-B", "mirror-source", "FETCH_HEAD"])
        if checkout.get("returncode") != 0:
            return {
                "repo": str(source),
                "mirror": False,
                "error": {
                    "type": "MirrorError",
                    "message": (
                        (checkout.get("stderr") or checkout.get("stdout") or "")[:400]
                    ),
                },
            }

        if not _is_clean(mirror):
            return {
                "repo": str(source),
                "mirror": False,
                "error": {
                    "type": "DirtyRepo",
                    "message": "managed mirror could not be established clean",
                },
            }
        head = _git(mirror, ["rev-parse", "HEAD"])
        return {
            "repo": str(mirror),
            "mirror": True,
            "error": None,
            "source_repo": str(source),
            "source_head": (head.get("stdout") or "").strip(),
        }
    except Exception as exc:  # defensive: mirror problems must not crash the engine
        return {
            "repo": str(source),
            "mirror": False,
            "error": {
                "type": "MirrorError",
                "message": f"{type(exc).__name__}: {exc}"[:400],
            },
        }
    finally:
        # No lock held across network/clone operations by callers; the
        # with-block only guards mirror bookkeeping.
        pass


def cleanup_mirror(repo: str) -> None:
    """Best-effort removal of the mirror for one source repo."""
    source = Path(repo).expanduser().resolve()
    root = managed_root() / _safe_dir_name(source)
    shutil.rmtree(root, ignore_errors=True)
