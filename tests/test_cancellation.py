"""
Stage 8.4: cancellation semantics.

- QUEUED: cancel is immediate; nothing ever executes
- RUNNING: cancellation is cooperative; the worker stops at the
  next checkpoint, restores, and never commits
- the source repository always stays safe
"""

import subprocess
import time
from pathlib import Path

import pytest

from app.core.models import Mission, MissionStatus
from app.storage.sqlite_store import MissionStore
from app.runtime.repo_leases import RepoLeaseManager
from app.runtime.coordinator import Coordinator

import app.workers.repo_code_worker as worker_module


TERMINAL = (MissionStatus.passed, MissionStatus.failed, MissionStatus.cancelled)


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


def make_coordinator(tmp_path, worker):
    db = tmp_path / "c.sqlite"
    store = MissionStore(db)
    leases = RepoLeaseManager(db)
    coordinator = Coordinator(
        store=store,
        leases=leases,
        registry=Registry(worker),
        id_prefix="cancel",
    )
    return coordinator, store


def wait_terminal(store, timeout=15):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        missions = store.list()

        if missions and missions[0].status in TERMINAL:
            return missions[0]

        time.sleep(0.05)

    raise AssertionError("mission never reached a terminal state")


def test_queued_cancel_is_immediate_and_never_executes(tmp_path):
    executed = []

    class Worker:
        name = "w"
        capabilities = {"repo-code"}

        def health(self):
            return {}

        def execute(self, goal, metadata=None):
            executed.append(goal)

            return {"success": True, "output": {}, "evidence": []}

    coordinator, store = make_coordinator(tmp_path, Worker())

    mission = Mission(
        goal="queued cancel",
        capability="repo-code",
        metadata={"repo_path": str(tmp_path)},
    )
    store.enqueue(mission)

    assert store.request_cancel(mission.id) == "cancelled"

    fetched = store.get(mission.id)

    assert fetched.status == MissionStatus.cancelled

    coordinator.start()
    time.sleep(0.5)
    coordinator.stop()

    assert executed == [], "cancelled queued mission must not execute"

    events = [e["event_type"] for e in store.events(mission.id)]

    assert "mission.cancelled" in events


def test_cancel_during_llm_call_stops_before_edits(tmp_path):
    repo = tmp_path / "repo"
    baseline = init_repo(repo)

    cancel_event = threading_event = __import__("threading").Event()

    def slow_llm_plan(goal, worktree, lessons=""):
        # Give the test time to request cancellation mid-call.
        cancel_event.wait(timeout=5)
        time.sleep(0.2)

        return {
            "action": "edit",
            "edits": [
                {
                    "target_file": "greet.py",
                    "find": "return 'Hello ' + name",
                    "replace": "return f'Hello {name}'",
                }
            ],
            "reason": "never applied",
        }

    original_plan = worker_module.generate_edit_plan
    worker_module.generate_edit_plan = slow_llm_plan

    class Worker:
        name = "w"
        capabilities = {"repo-code"}

        def health(self):
            return {}

        def execute(self, goal, metadata=None):
            return worker_module.RepoCodeWorker().execute(goal, metadata)

    coordinator, store = make_coordinator(tmp_path, Worker())
    coordinator.heartbeat_interval = 1

    store.enqueue(
        Mission(
            goal="will be cancelled mid-flight",
            capability="repo-code",
            metadata={"repo_path": str(repo)},
        )
    )

    coordinator.start()
    time.sleep(0.4)

    assert store.request_cancel(store.list()[0].id) == "requested"

    cancel_event.set()

    mission = wait_terminal(store)
    coordinator.stop()
    worker_module.generate_edit_plan = original_plan

    assert mission.status == MissionStatus.cancelled, mission.result
    assert "Cancelled" in mission.result["error"]["type"]

    # Source repository untouched; no branch, no commit.
    assert git(repo, "rev-parse", "HEAD") == baseline

    events = [e["event_type"] for e in store.events(mission.id)]

    assert "mission.cancel_requested" in events
    assert "mission.cancelled" in events


def test_cancel_requested_during_validation_prevents_commit(tmp_path):
    repo = tmp_path / "repo2"
    baseline = init_repo(repo)

    original_plan = worker_module.generate_edit_plan
    original_validation = worker_module.run_validation

    def plan_ok(goal, worktree, lessons=""):
        return {
            "action": "edit",
            "edits": [
                {
                    "target_file": "greet.py",
                    "find": "return 'Hello ' + name",
                    "replace": "return f'Hello {name}'",
                }
            ],
            "reason": "valid plan",
        }

    def cancelling_validation(worktree, evidence, test_commands):
        # Validation passes, but cancellation arrives before the
        # worker reaches the commit checkpoint.
        result = original_validation(worktree, evidence, test_commands)
        return result

    worker_module.generate_edit_plan = plan_ok

    store_holder = {}

    def validation_with_cancel(worktree, evidence, test_commands):
        store = store_holder["store"]
        mission_id = store_holder["mission_id"]
        store.request_cancel(mission_id)
        return original_validation(worktree, evidence, test_commands)

    worker_module.run_validation = validation_with_cancel

    class Worker:
        name = "w"
        capabilities = {"repo-code"}

        def health(self):
            return {}

        def execute(self, goal, metadata=None):
            return worker_module.RepoCodeWorker().execute(goal, metadata)

    coordinator, store = make_coordinator(tmp_path, Worker())
    store_holder["store"] = store

    store.enqueue(
        Mission(
            goal="cancel before commit",
            capability="repo-code",
            metadata={"repo_path": str(repo)},
        )
    )
    store_holder["mission_id"] = store.list()[0].id

    coordinator.start()
    mission = wait_terminal(store)
    coordinator.stop()

    worker_module.generate_edit_plan = original_plan
    worker_module.run_validation = original_validation

    assert mission.status == MissionStatus.cancelled

    # No commit anywhere: source at baseline; any leftover mission
    # branch (kept for debugging) still points at the baseline.
    assert git(repo, "rev-parse", "HEAD") == baseline

    branches = subprocess.run(
        ["git", "branch", "--list", "yodaw/*", "--format=%(refname:short) %(objectname)"],
        cwd=repo,
        text=True,
        capture_output=True,
    ).stdout.strip()

    for line in branches.splitlines():
        assert line.endswith(baseline), (
            f"cancelled mission left a commit behind: {line}"
        )


def test_terminal_mission_cannot_be_cancelled(tmp_path):
    coordinator, store = make_coordinator(tmp_path, object())

    mission = Mission(goal="done already", capability="repo-code")
    mission.status = MissionStatus.passed
    mission.finished_at = "already"
    store.save(mission)

    assert store.request_cancel(mission.id) == "terminal"
