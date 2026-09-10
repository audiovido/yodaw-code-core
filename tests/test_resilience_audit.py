"""
Worker J: resilience and queue-safety audit.

Hermetic: real SQLite, real threads, fake workers.
No network, no model, no API keys.

Covers:
- concurrent duplicate submissions (single-flight under races)
- idempotency races on outbox enqueue with keys
- outbox `pending` stat semantics (retry-scheduled is not due;
  dead-lettered is never pending)
- outbox duplicate delivery (two relays, one winner)
- stale lease/claim recovery and cross-process repo exclusion
- lease release isolation (release_all only releases own)
- concurrent writer safety on the queue
- worktree lifecycle: success removal, failure retention,
  cleanup failure never hides the result
- partial git apply: invalid second edit applies nothing
- dirty repo protection refuses before any mutation
"""

import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.core.models import Mission, MissionStatus
from app.learning.engine import record_id_default
from app.learning.store import LearningStore
from app.runtime.coordinator import Coordinator
from app.runtime.outbox_relay import OutboxRelay
from app.runtime.repo_leases import RepoLeaseManager
from app.storage.sqlite_store import (
    DuplicateMission,
    MissionStore,
    OUTBOX_MAX_ATTEMPTS,
)


# ---------------------------------------------------------
# Duplicate submissions under concurrency
# ---------------------------------------------------------

def test_duplicate_submission_rejected_sequentially(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    meta = {"repo_path": "/repo/x"}

    first = Mission(
        goal="same goal", capability="repo-code", metadata=dict(meta)
    )
    store.enqueue(first)

    with pytest.raises(DuplicateMission):
        store.enqueue(
            Mission(
                goal="same goal", capability="repo-code",
                metadata=dict(meta),
            )
        )

    # Different goal, same repo: a different work item, allowed.
    store.enqueue(
        Mission(
            goal="other goal", capability="repo-code",
            metadata=dict(meta),
        )
    )

    # After the first reaches terminal, the same goal resubmits.
    assert store.request_cancel(first.id) == "cancelled"
    store.enqueue(
        Mission(
            goal="same goal", capability="repo-code",
            metadata=dict(meta),
        )
    )


def test_concurrent_duplicate_submissions_single_flight(tmp_path):
    # Four threads submit the identical work item at once: exactly
    # one insert must win; the losers get DuplicateMission.
    store = MissionStore(tmp_path / "db.sqlite")
    meta = {"repo_path": "/repo/race"}

    barrier = threading.Barrier(4)
    outcomes = []
    lock = threading.Lock()

    def submit():
        barrier.wait()
        try:
            mission = Mission(
                goal="same goal", capability="repo-code",
                metadata=dict(meta),
            )
            store.enqueue(mission)
            with lock:
                outcomes.append("ok")
        except DuplicateMission:
            with lock:
                outcomes.append("dup")

    threads = [threading.Thread(target=submit) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["dup", "dup", "dup", "ok"]
    assert len(store.list()) == 1


# ---------------------------------------------------------
# Outbox stat semantics + duplicate delivery
# ---------------------------------------------------------

def test_pending_stat_excludes_dead_lettered(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    message_id = store.outbox_enqueue(
        mission_id="m1",
        kind="learning.record",
        payload={"goal": "g", "success": True},
    )

    assert store.outbox_stats()["pending"] == 1

    store.outbox_dead_letter(message_id)

    # A dead-lettered message is quarantined for operator review:
    # it must never be counted as deliverable work again.
    stats = store.outbox_stats()
    assert stats["pending"] == 0
    assert stats["dead_lettered"] == 1
    assert stats["delivered"] == 0
    assert store.outbox_pending() == []


def test_retry_scheduled_message_is_pending_but_not_due(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    relay = OutboxRelay(store=store)

    message_id = store.outbox_enqueue(
        mission_id="m2",
        kind="learning.record",
        payload={
            "goal": "g",
            "success": True,
            "record_id": record_id_default("m2"),
        },
    )

    def failing(payload, sink=None):
        raise RuntimeError("destination outage")

    from app.runtime import outbox_relay as relay_module

    original = relay_module._HANDLERS.get("learning.record")
    relay_module._HANDLERS["learning.record"] = failing
    try:
        assert relay.drain_once() == 0
    finally:
        relay_module._HANDLERS["learning.record"] = original

    # Not lost (pending count holds it), not redelivered early
    # (backoff schedules the retry), error recorded precisely.
    assert store.outbox_stats()["pending"] == 1
    assert store.outbox_pending() == []
    message = store.outbox_message(message_id)
    assert message["attempts"] == 1
    assert "destination outage" in message["last_error"]
    assert message["next_attempt_at"] is not None
    assert message["dead_lettered_at"] is None


def test_two_relays_deliver_once_single_winner(tmp_path):
    from app.learning.engine import record_id_default

    store = MissionStore(tmp_path / "db.sqlite")
    store.outbox_enqueue(
        mission_id="m3",
        kind="learning.record",
        payload={
            "goal": "dup safe",
            "success": True,
            "record_id": record_id_default("m3"),
        },
    )

    relay_a = OutboxRelay(store=store)
    relay_b = OutboxRelay(store=store)
    results = []

    def drain(relay):
        results.append(relay.drain_once())

    threads = [
        threading.Thread(target=drain, args=(relay_a,)),
        threading.Thread(target=drain, args=(relay_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1
    assert store.outbox_stats()["delivered"] == 1
    assert store.outbox_stats()["pending"] == 0


def test_concurrent_idempotent_enqueue_single_row(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    ids = []

    def enqueue():
        ids.append(
            store.outbox_enqueue(
                mission_id="m",
                kind="learning.record",
                payload={"goal": "g", "success": True},
                idempotency_key="race-key",
            )
        )

    threads = [threading.Thread(target=enqueue) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(ids) == {ids[0]}
    assert store.outbox_stats()["total"] == 1


def test_relay_restart_picks_up_orphaned_message(tmp_path):
    db_path = tmp_path / "db.sqlite"
    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    store.outbox_enqueue(
        mission_id="m_orphan",
        kind="learning.record",
        payload={
            "mission_id": "m_orphan",
            "goal": "orphaned learning",
            "success": True,
            "record_id": record_id_default("m_orphan"),
        },
    )

    # "Crash": relay A never drains. A fresh relay delivers.
    assert OutboxRelay(store=store).drain_once() == 1
    records = [r for r in learning.list() if r.mission_id == "m_orphan"]
    assert len(records) == 1


def test_poison_message_dead_letters_after_bounded_attempts(tmp_path):
    import sqlite3

    store = MissionStore(tmp_path / "db.sqlite")
    relay = OutboxRelay(store=store)

    message_id = store.outbox_enqueue(
        mission_id="m_poison",
        kind="no.handler.kind",
        payload={"x": 1},
    )

    for _ in range(OUTBOX_MAX_ATTEMPTS):
        db = sqlite3.connect(str(store.path))
        db.execute(
            "UPDATE mission_outbox SET next_attempt_at="
            "'2000-01-01T00:00:00+00:00'"
        )
        db.commit()
        db.close()
        relay.drain_once()

    message = store.outbox_message(message_id)
    assert message["dead_lettered_at"] is not None
    assert message["attempts"] == OUTBOX_MAX_ATTEMPTS
    assert store.outbox_stats()["pending"] == 0
    assert store.outbox_stats()["dead_lettered"] == 1
    assert store.outbox_message(message_id)["payload"] == {"x": 1}


# ---------------------------------------------------------
# Stale lease / claim recovery + exclusion
# ---------------------------------------------------------

def test_stale_lease_is_stolen_by_new_owner(tmp_path):
    import sqlite3

    db = tmp_path / "db.sqlite"
    first = RepoLeaseManager(db)
    second = RepoLeaseManager(db)

    repo = str(tmp_path / "shared")
    assert first.acquire(repo, "proc-a") is True
    assert second.acquire(repo, "proc-b") is False

    stale = sqlite3.connect(str(db))
    stale.execute(
        "UPDATE repo_leases SET heartbeat_at="
        "'2020-01-01T00:00:00+00:00' WHERE repo_key=?",
        (repo,),
    )
    stale.commit()
    stale.close()

    assert second.acquire(repo, "proc-b", stale_after_seconds=300) is True
    assert first.acquire(repo, "proc-a", stale_after_seconds=300) is False

    second.release(repo, "proc-b")
    assert first.acquire(repo, "proc-a") is True


def test_lease_heartbeat_guarded_by_owner(tmp_path):
    db = tmp_path / "db.sqlite"
    first = RepoLeaseManager(db)
    second = RepoLeaseManager(db)

    repo = str(tmp_path / "owned")
    assert first.acquire(repo, "owner-a") is True
    assert second.heartbeat(repo, "owner-b") is False
    assert first.heartbeat(repo, "owner-a") is True

    # A foreign release is a no-op; the owner's lease survives.
    second.release(repo, "owner-b")
    assert first.holder(repo) == "owner-a"


def test_release_all_only_releases_own_leases(tmp_path):
    db = tmp_path / "db.sqlite"
    first = RepoLeaseManager(db)
    second = RepoLeaseManager(db)

    first.acquire(str(tmp_path / "r1"), "owner-a")
    second.acquire(str(tmp_path / "r2"), "owner-b")

    first.release_all("owner-a")

    assert first.held_by("owner-a") == set()
    assert second.held_by("owner-b") == {str(tmp_path / "r2")}


def test_two_coordinators_claim_exactly_once(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    total = 15
    for i in range(total):
        store.enqueue(
            Mission(
                goal=f"exact {i}",
                capability="repo-code",
                metadata={"repo_path": f"/repo/{i}"},
            )
        )

    claimed = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def claimer(name):
        barrier.wait()
        while True:
            mission = store.claim_next(name)
            if mission is None:
                return
            with lock:
                claimed.append(mission.id)

    threads = [
        threading.Thread(target=claimer, args=(f"cc-{i}",))
        for i in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == total
    assert len(set(claimed)) == total


def test_same_repo_never_executes_concurrently(tmp_path):
    class SlowWorker:
        name = "slow-bud"
        capabilities = {"repo-code"}

        def execute(self, goal, metadata=None):
            time.sleep(0.5)
            return {
                "success": True,
                "output": {"goal": goal},
                "evidence": [],
                "error": None,
            }

    class Registry:
        def find(self, capability):
            return SlowWorker()

        def status(self):
            return []

    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)
    coordinator = Coordinator(
        store=store,
        leases=leases,
        registry=Registry(),
        id_prefix="excl",
        relay=OutboxRelay(store=store),
    )
    coordinator.max_concurrent = 4

    repo = str(tmp_path / "same")
    for i in range(2):
        store.enqueue(
            Mission(
                goal=f"same-repo-{i}",
                capability="repo-code",
                metadata={"repo_path": repo},
            )
        )

    coordinator.start()
    time.sleep(0.25)

    with coordinator._inflight_lock:
        repos = set(coordinator._inflight_repos)
        inflight = set(coordinator._inflight)

    # Both slots may fill, but never with the same repository.
    assert len(repos) == len(inflight)

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if all(
            m.status in (MissionStatus.passed, MissionStatus.failed)
            for m in store.list()
        ):
            break
        time.sleep(0.05)

    coordinator.stop()
    assert all(m.status == MissionStatus.passed for m in store.list())


# ---------------------------------------------------------
# Temp worktree lifecycle + git safety
# ---------------------------------------------------------

def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repo(repo: Path, files: dict):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")
    for name, content in files.items():
        (repo / name).write_text(content)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return git(repo, "rev-parse", "HEAD")


def test_successful_mission_removes_worktree(tmp_path):
    from app.workers.repo_code_worker import RepoCodeWorker

    repo = tmp_path / "repo_success"
    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 2\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    assert result["success"] is True, result
    assert not Path(result["output"]["worktree"]).exists()

    cleanups = [
        item for item in result["evidence"]
        if isinstance(item, dict) and item.get("type") == "worktree_cleanup"
    ]
    assert cleanups and cleanups[0]["action"] == "removed"
    # Source repository never mutated (commit lives on the branch).
    assert (repo / "calc.py").read_text() == "def value():\n    return 1\n"


def test_failed_mission_keeps_worktree_clean_for_debugging(tmp_path):
    import app.workers.repo_code_worker as worker_module
    from app.workers.repo_code_worker import RepoCodeWorker

    repo = tmp_path / "repo_failed"
    baseline = init_repo(
        repo,
        {
            "a.py": "VALUE = 1\n",
            "b.py": "VALUE = 10\n",
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    result = RepoCodeWorker().execute(
        "Atomic failure test.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "a.py",
                    "find": "VALUE = 1",
                    "replace": "VALUE = 2",
                },
                {
                    "target_file": "b.py",
                    "find": "THIS DOES NOT EXIST",
                    "replace": "VALUE = 20",
                },
            ],
        },
    )

    assert result["success"] is False

    worktree = Path(result["output"]["worktree"])
    cleanups = [
        item for item in result["evidence"]
        if isinstance(item, dict) and item.get("type") == "worktree_cleanup"
    ]
    assert cleanups and cleanups[0]["action"] == "kept_failed_for_debugging"

    # Failed worktree restored to baseline and clean.
    assert worktree.exists()
    assert worker_module.worktree_is_clean(worktree) is True
    assert (worktree / "a.py").read_text() == "VALUE = 1\n"
    assert (worktree / "b.py").read_text() == "VALUE = 10\n"
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_cleanup_failure_does_not_hide_mission_result(tmp_path, monkeypatch):
    import app.workers.repo_code_worker as worker_module
    from app.workers.repo_code_worker import RepoCodeWorker

    repo = tmp_path / "repo_cleanup_fail"
    init_repo(
        repo,
        {
            "calc.py": "def value():\n    return 1\n",
            "test_calc.py": (
                "from calc import value\n\n\n"
                "def test_value():\n    assert value() == 2\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    real_run = worker_module.run

    def failing_git_remove(cmd, cwd=None, timeout=300):
        if "worktree" in cmd and "remove" in cmd:
            return {
                "cmd": " ".join(cmd),
                "cwd": str(cwd) if cwd else None,
                "stdout": "",
                "stderr": "simulated git worktree remove failure",
                "returncode": 1,
                "timestamp": "simulated",
            }
        return real_run(cmd, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(worker_module, "run", failing_git_remove)

    result = RepoCodeWorker().execute(
        "Bump value.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "calc.py",
                    "find": "return 1",
                    "replace": "return 2",
                }
            ],
        },
    )

    assert result["success"] is True, result
    errors = [
        item for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "worktree_cleanup_error"
    ]
    assert errors
    assert not Path(result["output"]["worktree"]).exists()


def test_partial_apply_second_edit_invalid_applies_nothing(tmp_path):
    import app.workers.repo_code_worker as worker_module
    from app.workers.repo_code_worker import RepoCodeWorker

    repo = tmp_path / "repo_atomic"
    baseline = init_repo(
        repo,
        {
            "a.py": "VALUE = 1\n",
            "b.py": "VALUE = 10\n",
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    result = RepoCodeWorker().execute(
        "Atomic failure test.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "a.py",
                    "find": "VALUE = 1",
                    "replace": "VALUE = 2",
                },
                {
                    "target_file": "b.py",
                    "find": "THIS DOES NOT EXIST",
                    "replace": "VALUE = 20",
                },
            ],
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "FindTextMissing"

    # All-or-nothing: the first (valid) edit was staged in memory
    # only and never reached the filesystem.
    worktree = Path(result["output"]["worktree"])
    assert (worktree / "a.py").read_text() == "VALUE = 1\n"
    assert worker_module.worktree_is_clean(worktree) is True
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_dirty_repo_refuses_before_any_mutation(tmp_path):
    from app.workers.repo_code_worker import RepoCodeWorker

    repo = tmp_path / "repo_dirty"
    init_repo(repo, {"a.py": "VALUE = 1\n"})
    (repo / "a.py").write_text("VALUE = DIRTY UNCOMMITTED\n")

    result = RepoCodeWorker().execute(
        "Any goal.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "a.py",
                    "find": "VALUE",
                    "replace": "VALUE = 2",
                }
            ],
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "DirtyRepo"
    assert result["retryable"] is False
    # The dirty content is untouched: refusal happens before work.
    assert (repo / "a.py").read_text() == "VALUE = DIRTY UNCOMMITTED\n"


def test_path_escape_rejected_before_any_write(tmp_path):
    from app.workers.repo_code_worker import RepoCodeWorker

    repo = tmp_path / "repo_escape"
    baseline = init_repo(repo, {"a.py": "VALUE = 1\n"})
    outside = tmp_path / "outside_escape_probe.txt"
    outside.write_text("untouched")

    result = RepoCodeWorker().execute(
        "Escape attempt.",
        {
            "repo_path": str(repo),
            "edits": [
                {
                    "target_file": "../outside_escape_probe.txt",
                    "find": "untouched",
                    "replace": "hacked",
                }
            ],
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "PathEscapeError"
    assert outside.read_text() == "untouched"
    assert git(repo, "rev-parse", "HEAD") == baseline
