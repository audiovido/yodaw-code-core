"""Safe isolated-worktree lifecycle: allocation, fencing, cleanup.

Allocation produces a unique worktree directory and branch for each
call (timestamp + pid + counter + random suffix), removes stale
leftover directories from previous runs, and never reuses an existing
branch. The commit fence checks, before edits and before commit, that
the worktree is clean and the source repository HEAD has not moved.
Cleanup mirrors the production worker policy: success removes the
worktree; failure or keep_worktree retains it with a recorded reason.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Callable, Optional

from app.workers.mission_evidence import now_iso
from app.workers.safe_subprocess import run as _default_run


class FenceViolation(RuntimeError):
    """The worktree or source repo violated a pre-commit fence check."""


_lock = threading.Lock()
_counter = 0


def _unique_suffix() -> str:
    global _counter
    with _lock:
        _counter += 1
        return "%d-%d-%d-%s" % (
            os.getpid(),
            _counter,
            int(datetime.now(timezone.utc).timestamp()),
            uuid.uuid4().hex[:8],
        )


def allocate(
    repo,
    worktree_root,
    branch_name: Optional[str] = None,
    *,
    run: Optional[Callable] = None,
    check_clean: bool = True,
) -> dict:
    """Allocate a fresh isolated worktree and branch.

    Returns {"worktree", "branch", "base_sha", "error", "stale_removed"}.
    error is a dict (or None) shaped like the worker error contract.
    Raises WorkerError-producing conditions as error dicts, never
    touching the source tree beyond listing/reading.
    """
    run = run or _default_run
    repo = Path(repo)
    worktree_root = Path(worktree_root)

    if not (repo / ".git").exists():
        return _error(
            {"type": "RepoError", "message": "Target path is not a git repository"}
        )

    if check_clean:
        status = run(["git", "status", "--short"], cwd=str(repo))
        if status["returncode"] != 0 or status["stdout"].strip():
            return _error(
                {
                    "type": "DirtyRepo",
                    "message": "Source repository has uncommitted changes",
                }
            )

    base_sha = run(["git", "rev-parse", "HEAD"], cwd=str(repo))
    if base_sha["returncode"] != 0:
        return _error(
            {
                "type": "RepoError",
                "message": base_sha["stderr"] or base_sha["stdout"],
            }
        )

    worktree_root.mkdir(parents=True, exist_ok=True)

    branch = branch_name or _default_branch_name()
    if branch_name is not None and _branch_exists(repo, branch_name, run):
        # Stale branch handling: never overwrite an existing branch.
        branch = "%s-%s" % (branch_name, _unique_suffix())

    worktree = worktree_root / ("repo_%s" % _unique_suffix())

    # Stale directory handling: directories that look like leftovers
    # from crashed runs (repo_*) but are not registered as git
    # worktrees are removed so allocations never collide with them.
    stale_removed = 0
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
        stale_removed += 1
    for leftover in _unregistered_leftovers(worktree_root, repo, run):
        shutil.rmtree(leftover, ignore_errors=True)
        stale_removed += 1

    add_result = run(
        ["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"],
        cwd=str(repo),
    )
    if add_result["returncode"] != 0:
        return _error(
            {
                "type": "WorktreeError",
                "message": add_result["stderr"] or add_result["stdout"],
            }
        )

    return {
        "worktree": worktree,
        "branch": branch,
        "base_sha": base_sha["stdout"].strip(),
        "error": None,
        "stale_removed": stale_removed > 0,
    }


def _default_branch_name() -> str:
    return "yodaw/task-%s" % _unique_suffix()


def _unregistered_leftovers(worktree_root: Path, repo: Path, run: Callable) -> list:
    """Directories under worktree_root named repo_* that git no longer
    registers as worktrees (crashed runs leave these behind)."""
    registered = set()
    listing = run(["git", "worktree", "list", "--porcelain"], cwd=str(repo))
    for line in listing.get("stdout", "").splitlines():
        if line.startswith("worktree "):
            registered.add(line[len("worktree "):].strip())
    leftovers = []
    if not worktree_root.exists():
        return leftovers
    for child in worktree_root.iterdir():
        if not child.is_dir() or not child.name.startswith("repo_"):
            continue
        try:
            resolved = child.resolve()
        except OSError:
            continue
        if str(resolved) not in registered:
            leftovers.append(child)
    return leftovers


def _branch_exists(repo: Path, branch: str, run: Callable) -> bool:
    result = run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/heads/%s" % branch],
        cwd=str(repo),
    )
    return result["returncode"] == 0 or "refs/heads/%s" % branch in result.get(
        "stdout", ""
    )


def verify_commit_fence(
    repo,
    worktree,
    base_sha: Optional[str] = None,
    *,
    run: Optional[Callable] = None,
    expect_clean: bool = True,
) -> None:
    """Enforce the commit fence; raise FenceViolation when violated.

    Checks:
    - the source repository HEAD still matches base_sha (never commit
      on top of a repo that moved during the mission), and
    - the worktree status matches expect_clean (dirty worktrees are
      never committed over).
    """
    run = run or _default_run
    worktree = Path(worktree)

    if base_sha is not None:
        head = run(["git", "rev-parse", "HEAD"], cwd=str(repo))
        if head.get("returncode") != 0 or head.get("stdout", "").strip() != base_sha:
            raise FenceViolation(
                "source repo HEAD moved away from base %s" % base_sha
            )

    status = run(["git", "status", "--short"], cwd=str(worktree))
    dirty = bool(status.get("stdout", "").strip())
    if expect_clean and dirty:
        raise FenceViolation("worktree is dirty at commit fence")
    if not expect_clean and not dirty:
        raise FenceViolation("worktree unexpectedly clean at commit fence")


def worktree_is_clean(worktree, *, run: Optional[Callable] = None) -> bool:
    """True when the worktree has no uncommitted changes."""
    run = run or _default_run
    status = run(["git", "status", "--short"], cwd=str(worktree))
    return status.get("returncode") == 0 and not status.get("stdout", "").strip()


def cleanup(
    repo,
    worktree,
    *,
    keep: bool = False,
    failed: bool = False,
    run: Optional[Callable] = None,
    evidence: Optional[list] = None,
) -> dict:
    """Remove (or retain) a worktree after a mission and record why.

    Mirror of the production worker cleanup policy:
    - keep=True retains the worktree for any outcome.
    - failed missions retain it for debugging (never silently).
    - successful missions remove it, prune, and fall back to a
      filesystem removal if git worktree remove fails.
    Returns a lifecycle dict; cleanup failure is recorded as evidence,
    never raised (the mission result stands).
    """
    run = run or _default_run
    evidence = [] if evidence is None else evidence

    if keep:
        action = "kept_by_request"
    elif failed:
        action = "kept_failed_for_debugging"
    else:
        action = "removed"

    record = {
        "type": "worktree_cleanup",
        "action": action,
        "worktree": str(worktree),
        "timestamp": now_iso(),
    }
    evidence.append(record)

    if action != "removed":
        return record

    try:
        remove_result = run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=str(repo),
        )
        evidence.append(remove_result)

        if remove_result["returncode"] != 0:
            evidence.append(
                {
                    "type": "worktree_cleanup_error",
                    "worktree": str(worktree),
                    "error": "git worktree remove failed; used filesystem fallback",
                    "timestamp": now_iso(),
                }
            )
            shutil.rmtree(worktree, ignore_errors=True)

        prune_result = run(["git", "worktree", "prune"], cwd=str(repo))
        evidence.append(prune_result)
        record["prune_returncode"] = prune_result.get("returncode")
    except Exception as exc:
        evidence.append(
            {
                "type": "worktree_cleanup_error",
                "worktree": str(worktree),
                "error": str(exc),
                "timestamp": now_iso(),
            }
        )
        try:
            shutil.rmtree(worktree, ignore_errors=True)
            run(["git", "worktree", "prune"], cwd=str(repo))
        except Exception:
            pass

    return record


def _error(error: dict) -> dict:
    return {
        "worktree": None,
        "branch": None,
        "base_sha": None,
        "error": error,
        "stale_removed": False,
    }