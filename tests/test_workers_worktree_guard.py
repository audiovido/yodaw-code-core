"""Focused tests for worktree_guard: unique allocation, stale branch
handling, commit fence, cleanup, and concurrent allocation."""

import shutil
import subprocess
import threading

import pytest

from app.workers.safe_subprocess import run
from app.workers.worktree_guard import (
    FenceViolation,
    _unique_suffix,
    allocate,
    cleanup,
    verify_commit_fence,
    worktree_is_clean,
)


def _git(repo, *args):
    result = run(["git", *args], cwd=str(repo))
    assert result["returncode"] == 0, result["stderr"]
    return result


def _make_repo(path):
    # Deterministic default branch: the suite must not depend on the
    # machine's init.defaultBranch (this host defaults to "main").
    _git(path, "-c", "init.defaultBranch=main", "init", "-q")
    _git(path, "config", "user.email", "test@yodaw.local")
    _git(path, "config", "user.name", "YODAW Test")
    (path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "seed.txt")
    _git(path, "commit", "-q", "-m", "seed")
    return path


def _make_repo_path(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    return _make_repo(repo)


@pytest.fixture()
def repo(tmp_path):
    return _make_repo_path(tmp_path)


def test_unique_allocation(repo, tmp_path):
    root = tmp_path / "worktrees"
    first = allocate(repo, root)
    second = allocate(repo, root)

    assert first["error"] is None
    assert second["error"] is None
    assert first["worktree"] != second["worktree"]
    assert first["branch"] != second["branch"]
    assert first["worktree"].exists()
    assert first["base_sha"]
    assert second["base_sha"] == first["base_sha"]
    assert worktree_is_clean(first["worktree"])


def test_stale_branch_handling(repo, tmp_path):
    _git(repo, "branch", "yodaw/stale-branch")
    root = tmp_path / "worktrees"
    allocated = allocate(repo, root, branch_name="yodaw/stale-branch")
    assert allocated["error"] is None
    assert allocated["branch"] != "yodaw/stale-branch"
    assert allocated["branch"].startswith("yodaw/stale-branch-")


def test_stale_directory_removed(repo, tmp_path):
    root = tmp_path / "worktrees"
    allocate(repo, root)
    # Simulate a leftover worktree dir from a prior crashed run.
    stale = root / "repo_stale_leftover"
    stale.mkdir(parents=True)
    (stale / "junk.txt").write_text("junk", encoding="utf-8")
    allocated = allocate(repo, root)
    assert allocated["error"] is None
    assert allocated["worktree"].exists()
    assert not (root / "repo_stale_leftover").exists()

def test_live_worktree_of_other_repo_never_pruned(tmp_path):
    """Allocation for one repo must never delete a live worktree of
    another repo sharing the same worktree root (concurrent missions
    on different repos run in one process)."""
    repo_a = _make_repo_path(tmp_path, "repo_a")
    repo_b = _make_repo_path(tmp_path, "repo_b")
    root = tmp_path / "worktrees"

    b_alloc = allocate(repo_b, root)
    assert b_alloc["error"] is None
    assert b_alloc["worktree"].exists()

    a_alloc = allocate(repo_a, root)
    assert a_alloc["error"] is None
    assert a_alloc["stale_removed"] is False
    # B's live, git-registered worktree is untouched.
    assert b_alloc["worktree"].exists()

    # B can still use the worktree end to end.
    (b_alloc["worktree"] / "change.txt").write_text("x", encoding="utf-8")
    _git(b_alloc["worktree"], "add", "change.txt")
    _git(b_alloc["worktree"], "commit", "-q", "-m", "still alive")
    assert worktree_is_clean(b_alloc["worktree"])


def test_dirty_repo_rejected(repo, tmp_path):
    (repo / "dirty.txt").write_text("x", encoding="utf-8")
    allocated = allocate(repo, tmp_path / "worktrees")
    assert allocated["error"] is not None
    assert allocated["error"]["type"] == "DirtyRepo"


def test_non_repo_rejected(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    allocated = allocate(plain, tmp_path / "worktrees")
    assert allocated["error"]["type"] == "RepoError"


def test_commit_fence_clean_passes(repo, tmp_path):
    allocated = allocate(repo, tmp_path / "worktrees")
    verify_commit_fence(repo, allocated["worktree"], allocated["base_sha"])


def test_commit_fence_dirty_worktree_raises(repo, tmp_path):
    allocated = allocate(repo, tmp_path / "worktrees")
    (allocated["worktree"] / "change.txt").write_text("x", encoding="utf-8")
    with pytest.raises(FenceViolation):
        verify_commit_fence(repo, allocated["worktree"], allocated["base_sha"])


def test_commit_fence_moved_base_raises(repo, tmp_path):
    allocated = allocate(repo, tmp_path / "worktrees")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "extra.txt").write_text("y", encoding="utf-8")
    _git(repo, "add", "extra.txt")
    _git(repo, "commit", "-q", "-m", "move base")
    with pytest.raises(FenceViolation):
        verify_commit_fence(repo, allocated["worktree"], allocated["base_sha"])
    _git(repo, "checkout", "-q", "main")


def test_cleanup_removes_success(repo, tmp_path):
    allocated = allocate(repo, tmp_path / "worktrees")
    worktree = allocated["worktree"]
    evidence = []
    record = cleanup(repo, worktree, run=run, evidence=evidence)

    assert record["action"] == "removed"
    assert not worktree.exists()
    assert not (repo / ".git" / "worktrees").joinpath(worktree.name).exists()
    # The worktree is no longer registered with git.
    listing = run(["git", "worktree", "list"], cwd=str(repo))
    assert str(worktree) not in listing["stdout"]


def test_cleanup_keeps_on_failure(repo, tmp_path):
    allocated = allocate(repo, tmp_path / "worktrees")
    worktree = allocated["worktree"]
    record = cleanup(repo, worktree, failed=True)
    assert record["action"] == "kept_failed_for_debugging"
    assert worktree.exists()


def test_cleanup_kept_by_request(repo, tmp_path):
    allocated = allocate(repo, tmp_path / "worktrees")
    worktree = allocated["worktree"]
    record = cleanup(repo, worktree, keep=True)
    assert record["action"] == "kept_by_request"
    assert worktree.exists()


def test_cleanup_records_evidence(repo, tmp_path):
    allocated = allocate(repo, tmp_path / "worktrees")
    evidence = []
    cleanup(repo, allocated["worktree"], evidence=evidence)
    assert any(event["type"] == "worktree_cleanup" for event in evidence)


def test_concurrent_allocation_suffixes_are_unique():
    suffixes = []
    errors = []

    def worker():
        try:
            for _ in range(20):
                suffixes.append(_unique_suffix())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(suffixes) == 160
    assert len(set(suffixes)) == 160


def test_concurrent_allocate_against_same_repo(repo, tmp_path):
    root = tmp_path / "worktrees"
    outcomes = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        outcomes.append(allocate(repo, root))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    allocated = [o for o in outcomes if o["error"] is None]
    pairs = [(o["worktree"], o["branch"]) for o in allocated]
    assert len(pairs) == len(set(pairs)), "allocations must be unique"
    assert len(pairs) >= 1