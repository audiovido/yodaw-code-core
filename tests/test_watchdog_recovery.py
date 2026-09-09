"""
Stage 8.5: heartbeat watchdog and crash recovery.

A crashed coordinator leaves a RUNNING mission with a frozen
heartbeat. Recovery must inspect (never blindly re-run) and fail
the mission with an explicit InterruptedExecution error, reporting
exactly what was left in the target repository.
"""

import subprocess
import time
from pathlib import Path

from app.core.models import Mission, MissionStatus
from app.storage.sqlite_store import MissionStore
from app.runtime.repo_leases import RepoLeaseManager
from app.runtime.coordinator import Coordinator


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repo(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return git(repo, "rev-parse", "HEAD")


class Registry:
    def __init__(self, worker):
        self.worker = worker

    def find(self, capability):
        return self.worker

    def status(self):
        return []


def init_repo_clean(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return git(repo, "rev-parse", "HEAD")


def test_stale_executing_detected_by_cutoff(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(goal="g", capability="repo-code")
    store.enqueue(mission)
    store.claim_next("dead-coordinator")

    assert len(store.stale_executing(0)) == 1
    assert len(store.stale_executing(3600)) == 0


def test_crash_recovery_fails_mission_without_reexecution(tmp_path):
    repo = tmp_path / "repo"
    baseline = init_repo(repo)

    db = tmp_path / "db.sqlite"
    store = MissionStore(db)

    store.enqueue(
        Mission(
            goal="crash mid-flight",
            capability="repo-code",
            metadata={"repo_path": str(repo)},
        )
    )

    # Simulate a coordinator crash right after the claim: the
    # mission stays RUNNING with a stale heartbeat and a worktree
    # exists (as a real crashed run would leave behind).
    claimed = store.claim_next("dead-coordinator")

    worktree = Path(
        "/tmp/yodaw_crash_test_worktrees"
    ) / claimed.id
    worktree.parent.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            "yodaw/crash-test",
            str(worktree),
            "HEAD",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    claimed.result = {"branch": "yodaw/crash-test", "worktree": str(worktree)}

    # Age the heartbeat: the coordinator died hours ago.
    claimed.heartbeat_at = "2026-09-09T00:00:00+00:00"
    store.save(claimed)

    # A fresh coordinator (the "restarting" one) runs the watchdog.
    leases = RepoLeaseManager(db)
    fresh = Coordinator(
        store=store,
        leases=leases,
        registry=Registry(None),
        id_prefix="fresh",
    )

    recovered = fresh.recover_stale_missions()

    assert recovered == [claimed.id]

    mission = store.get(claimed.id)

    assert mission.status == MissionStatus.failed
    assert mission.result["error"]["type"] == "InterruptedExecution"
    assert mission.result["error"]["recovered_by"] == fresh.id

    # Evidence records the inspection: leftover branch + SHA.
    inspection = next(
        e
        for e in mission.evidence
        if e.get("type") == "recovery_inspection"
    )

    assert inspection["leftover_branch"] == "yodaw/crash-test"
    assert inspection["leftover_branch_sha"]
    assert inspection["claimed_by"] == "dead-coordinator"

    # Source repo HEAD untouched; the crash-test branch points at
    # the baseline because the dead coordinator never committed.
    assert git(repo, "rev-parse", "HEAD") == baseline

    sha = subprocess.run(
        ["git", "rev-parse", "--verify", "yodaw/crash-test"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    assert sha == baseline, "recovery must not re-run or commit"

    # Exactly one recovery event.
    events = [
        e["event_type"] for e in store.events(claimed.id)
    ]

    assert events.count("mission.recovered") == 1

    # Cleanup the simulated worktree.
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_watchdog_never_touches_live_heartbeat(tmp_path):
    db = tmp_path / "db.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)

    mission = Mission(goal="live mission", capability="repo-code")
    store.enqueue(mission)
    live = store.claim_next("live-coordinator")

    # Refresh the heartbeat so the mission is not stale.
    assert store.heartbeat(live.id, "live-coordinator") is True

    watchdog = Coordinator(
        store=store,
        leases=leases,
        registry=Registry(None),
        id_prefix="watchdog",
    )

    assert watchdog.recover_stale_missions() == []

    still = store.get(live.id)

    assert still.status == MissionStatus.running
    assert still.claimed_by == "live-coordinator"


def test_recovered_mission_claim_can_be_cleared_for_retry_later(tmp_path):
    """
    After InterruptedExecution, the mission is terminal. A human
    operator or future policy may resubmit the same goal; the old
    failed record stays intact for evidence.
    """
    store = MissionStore(tmp_path / "db.sqlite")

    mission = Mission(
        goal="recoverable goal",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)
    store.claim_next("dead")

    recovered = MissionStore(tmp_path / "db.sqlite")
    assert len(recovered.stale_executing(0)) == 1
