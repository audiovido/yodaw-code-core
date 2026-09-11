"""
Canonical repository identity for authorization, deduplication, and
leases.

A mission targets a server-local repository by path. Everything that
makes a decision about that repository — admission authorization,
the single-flight duplicate key, and the per-repository lease key —
must agree on *one* canonical identity, or the same directory can be
reached twice under two spellings and both protections evaporate.

This module is that single identity:

- `canonical_repo_path()` collapses symlinks, `..`, `.`, repeated
  separators, trailing slashes, `~`, and relative spellings into one
  absolute string. It uses `realpath`, so it works for paths that do
  not exist yet and it resolves symlinks *before* any authorization
  decision — a symlink inside an authorized root that points outside
  it is therefore judged on its real target.
- `repo_identity()` is the deduplication/lease key derived from that
  canonical path, falling back to the capability for missions with no
  repository.
- `authorized_repo_roots()` / `ensure_authorized()` implement the
  optional operator bound on which directories may be targeted.

Trust posture: with no authorized roots configured, no path bound is
imposed, which is the documented trusted single-node posture — the
only callers are authenticated identities on a node the operator
controls. Setting `YODAW_REPO_ROOTS` (os.pathsep separated) turns the
bound on, and any target outside every root is refused before a
mission is admitted and again before a worker touches the filesystem.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Union
from typing import Optional, Union
from typing import Optional, Union

ENV_REPO_ROOTS = "YODAW_REPO_ROOTS"


class RepoNotAllowed(Exception):
    """The requested target repository is outside every authorized root."""


def canonical_repo_path(
    repo_path: Optional[Union[str, os.PathLike]],
) -> Optional[str]:
    """
    Canonical, alias-free identity for one target repository.

    Returns None for an absent or blank path. The result is absolute
    with symlinks resolved, so `/tmp/repo`, `/tmp/repo/`,
    `/tmp/./repo`, `/tmp/other/../repo`, and a symlink to the same
    directory all produce one string.
    """
    if repo_path is None:
        return None

    raw = str(repo_path).strip()

    if not raw:
        return None

    expanded = os.path.expanduser(raw)

    if not os.path.isabs(expanded):
        expanded = os.path.abspath(expanded)

    return os.path.realpath(expanded)


def repo_identity(
    repo_path: str | os.PathLike | None = None,
    capability: str | None = None,
) -> str:
    """
    Deduplication/lease key for a mission target.

    Missions without a repository are keyed by capability, exactly as
    before this module existed.
    """
    canonical = canonical_repo_path(repo_path)

    if canonical:
        return canonical

    return f"capability:{capability}"


def authorized_repo_roots() -> tuple[str, ...]:
    """
    Operator-configured authorized repository roots.

    Empty when `YODAW_REPO_ROOTS` is unset or holds no usable entry,
    which means "no path bound" (trusted single-node posture).
    """
    raw = os.environ.get(ENV_REPO_ROOTS, "").strip()

    if not raw:
        return ()

    roots: list[str] = []

    for part in raw.split(os.pathsep):
        canonical = canonical_repo_path(part)

        if canonical and canonical not in roots:
            roots.append(canonical)

    return tuple(roots)


def is_within_roots(path: str, roots: tuple[str, ...]) -> bool:
    """True when `path` is one of `roots` or lives beneath one."""
    for root in roots:
        if path == root:
            return True

        if path.startswith(root.rstrip(os.sep) + os.sep):
            return True

    return False


def ensure_authorized(repo_path: str | os.PathLike | None) -> str | None:
    """
    Canonical target path, or `RepoNotAllowed`.

    The authorization decision is always made on the canonical path,
    so no alias spelling can step outside the configured roots.
    """
    canonical = canonical_repo_path(repo_path)

    if canonical is None:
        return None

    roots = authorized_repo_roots()

    if roots and not is_within_roots(canonical, roots):
        raise RepoNotAllowed(
            f"target repository {canonical!r} is outside the authorized "
            f"roots configured by {ENV_REPO_ROOTS}"
        )

    return canonical


def repo_roots_report() -> dict:
    """Non-secret summary of the repository bound, for readiness."""
    roots = authorized_repo_roots()

    return {
        "restricted": bool(roots),
        "root_count": len(roots),
    }


def path_exists(repo_path: str | os.PathLike | None) -> bool:
    canonical = canonical_repo_path(repo_path)

    return bool(canonical) and Path(canonical).exists()
