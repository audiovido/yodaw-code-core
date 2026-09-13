"""
Wave 2 runtime wiring: dependency claim fence, deadline/cancel
wiring through real subprocess execution, worker safety wiring,
and the evidence_report contract.

Targeted regression tests (plan A-M):

A. dependency claim race                  -> test_dependency_claim_race_single_winner
B. dependent cannot run before parent PASS
C. failed parent => BLOCKED_EXTERNAL
D. unknown dependency => 422
E. cancellation during a long subprocess
F. timeout during a long subprocess
G. no commit on timeout
H. no commit on cancellation
I. process-tree cleanup                   (covered by test_workers_safe_subprocess)
J. secret blocks commit
K. no-validation does not PASS
L. worktree collision safety              (covered by test_workers_worktree_guard)
M. evidence_report on all terminal outcomes
"""

import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.core.models import Mission, MissionStatus
from app.storage.sqlite_store import MissionStore, UnknownDependency
from app.runtime.repo_leases import RepoLeaseManager
from app.runtime.coordinator import Coordinator

import app.workers.repo_code_worker as worker_module
from app.workers.worker_errors import MissionCancelled, MissionTimeout

TERMINAL = (
    MissionStatus.passed,
    MissionStatus.failed,
    MissionStatus.blocked,
    MissionStatus.blocked_external,
    MissionStatus.cancelled,
)

EVIDENCE_KEYS = {
    "terminal",
    "commands",
    "files_changed",
    "test_results",
    "diff_summary",
    "commit_sha",
    "working_tree_clean",
    "warnings",
}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _init_repo(repo, *, slow_test=False):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "yodaw@test.local")
    _git(repo, "config", "user.name", "YODAW Test")

    (repo / "app.py").write_text("def greet():\n    return 'old'\n")
    (repo / "test_app.py").write_text(
        "from app import greet\n\n"
        "def test_greet():\n"
        "    assert greet() == 'hello'\n"
    )
    if slow_test:
        (repo / "test_slow.py").write_text(
            "import time\n\n"
            "def test_slow():\n"
            "    time.sleep(120)\n"
            "    assert True\n"
        )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return _git(repo, "rev-parse", "HEAD")


class _Registry:
    def __init__(self, worker):
        self.worker = worker

    def find(self, capability):
        return self.worker

    def status(self):
        return []


def _make_coordinator(tmp_path, worker):
    store = MissionStore(Path(tmp_path) / "c.sqlite")
    leases = RepoLeaseManager(store.path)
    coordinator = Coordinator(
        store=store,
        leases=leases,
        registry=_Registry(worker),
        id_prefix="w2",
    )
    return coordinator, store


def _real_worker():
    class W:
        name = "code-bud"
        capabilities = {"repo-code"}

        def health(self):
            return {}

        def execute(self, goal, metadata=None):
            return worker_module.RepoCodeWorker().execute(goal, metadata)

    return W()


def _id_for(store, goal):
    """Locate a mission id by goal (store.list() is newest-first)."""
    matches = [m.id for m in store.list() if m.goal == goal]
    assert matches, f"no mission with goal {goal!r}"
    assert len(matches) == 1, f"ambiguous goal {goal!r}"
    return matches[0]


def _wait_terminal(store, mission_id, timeout=60):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        mission = store.get(mission_id)

        if mission is not None and mission.status in TERMINAL:
            return mission

        time.sleep(0.05)

    raise AssertionError(f"mission {mission_id} never reached a terminal state")


# ---------------------------------------------------------------
# A. dependency claim race
# ---------------------------------------------------------------

def test_dependency_claim_race_single_winner(tmp_path):
    store = MissionStore(Path(tmp_path) / "r.sqlite")

    store.enqueue(
        Mission(
            goal="parent",
            capability="repo-code",
            metadata={"repo_path": "/x"},
        )
    )
    parent = store.get(_id_for(store, "parent"))
    parent.status = MissionStatus.passed
    store.save(parent)

    store.enqueue(
        Mission(
            goal="dependent of passed parent",
            capability="repo-code",
            metadata={
                "repo_path": "/x",
                "dependencies": [parent.id],
            },
        )
    )
    dep_id = _id_for(store, "dependent of passed parent")

    barrier = threading.Barrier(2)
    claims = []
    lock = threading.Lock()

    def claim():
        barrier.wait()
        mission = store.claim_next(f"c-{threading.get_ident()}")
        with lock:
            claims.append(mission)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    handed = [m for m in claims if m is not None]
    assert len(handed) == 1
    assert handed[0].id == dep_id
    # The winner owns it; the guarded claim never double-hands.
    assert store.get(dep_id).claimed_by is not None


# ---------------------------------------------------------------
# B. dependent cannot run before parent PASS
# ---------------------------------------------------------------

def test_dependent_not_claimable_until_parent_pass(tmp_path):
    store = MissionStore(Path(tmp_path) / "b.sqlite")

    store.enqueue(
        Mission(
            goal="parent",
            capability="repo-code",
            metadata={"repo_path": "/x"},
        )
    )
    parent_id = _id_for(store, "parent")

    store.enqueue(
        Mission(
            goal="dependent",
            capability="repo-code",
            metadata={
                "repo_path": "/x",
                "dependencies": [parent_id],
            },
        )
    )
    dep_id = _id_for(store, "dependent")

    # While the parent is active the dependent is never handed out.
    claimed = store.claim_next("c1")
    assert claimed is not None and claimed.id == parent_id
    assert store.claim_next("c1") is None
    assert store.get(dep_id).status == MissionStatus.queued

    # Only a durable PASS makes the dependent claimable.
    parent = store.get(parent_id)
    parent.status = MissionStatus.passed
    store.save(parent)

    dependent = store.claim_next("c1")
    assert dependent is not None
    assert dependent.id == dep_id
    assert dependent.metadata["dependencies"] == [parent_id]


# ---------------------------------------------------------------
# C. failed parent => BLOCKED_EXTERNAL
# ---------------------------------------------------------------

def test_failed_parent_blocks_queued_dependent_in_claim(tmp_path):
    store = MissionStore(Path(tmp_path) / "blk.sqlite")

    store.enqueue(
        Mission(
            goal="parent",
            capability="repo-code",
            metadata={"repo_path": "/x"},
        )
    )
    parent_id = _id_for(store, "parent")

    store.enqueue(
        Mission(
            goal="dependent",
            capability="repo-code",
            metadata={
                "repo_path": "/x",
                "dependencies": [parent_id],
            },
        )
    )
    dep_id = _id_for(store, "dependent")

    parent = store.get(parent_id)
    parent.status = MissionStatus.failed
    store.save(parent)

    # No QUEUED candidate remains claimable; the dependent was
    # transitioned to BLOCKED_EXTERNAL inside the claim transaction.
    assert store.claim_next("c1") is None

    dependent = store.get(dep_id)
    assert dependent.status == MissionStatus.blocked_external
    error = dependent.result.get("error") or {}
    assert error["type"] == "DependencyFailed"
    assert error["failed_parent"] == parent_id

    event_types = [e["event_type"] for e in store.events(dep_id)]
    assert event_types.count("mission.blocked") == 1


def test_blocking_never_clobbers_running_or_passed(tmp_path):
    store = MissionStore(Path(tmp_path) / "nc.sqlite")

    # RUNNING dependent: failure must not clobber the running state.
    store.enqueue(
        Mission(
            goal="parent",
            capability="repo-code",
            metadata={"repo_path": "/x"},
        )
    )
    parent_id = _id_for(store, "parent")

    store.enqueue(
        Mission(
            goal="dep-running",
            capability="repo-code",
            metadata={
                "repo_path": "/x",
                "dependencies": [parent_id],
            },
        )
    )
    running_id = _id_for(store, "dep-running")
    running = store.get(running_id)
    running.status = MissionStatus.running
    store.save(running)

    parent = store.get(parent_id)
    parent.status = MissionStatus.failed
    store.save(parent)

    assert store.claim_next("c1") is None
    assert store.get(running_id).status == MissionStatus.running

    # PASS dependent: failure must never un-pass a passed mission.
    store.enqueue(
        Mission(
            goal="dep-passed",
            capability="repo-code",
            metadata={
                "repo_path": "/x",
                "dependencies": [parent_id],
            },
        )
    )
    passed_id = _id_for(store, "dep-passed")
    passed = store.get(passed_id)
    passed.status = MissionStatus.passed
    store.save(passed)

    assert store.claim_next("c1") is None
    assert store.get(passed_id).status == MissionStatus.passed

    # No blocking event exists for either untouched mission.
    dry_events = [
        e["event_type"]
        for mid in (running_id, passed_id)
        for e in store.events(mid)
    ]
    assert "mission.blocked" not in dry_events


# ---------------------------------------------------------------
# D. unknown dependency => 422
# ---------------------------------------------------------------

def test_unknown_dependency_rejected_at_enqueue(tmp_path):
    store = MissionStore(Path(tmp_path) / "u.sqlite")

    with pytest.raises(UnknownDependency):
        store.enqueue(
            Mission(
                goal="depends on ghost",
                capability="repo-code",
                metadata={
                    "repo_path": "/x",
                    "dependencies": ["m_does_not_exist"],
                },
            )
        )


def test_api_unknown_dependency_returns_422():
    from tests.test_api_v1_mission_product import make_client

    client = make_client()
    response = client.post(
        "/api/v1/missions",
        json={
            "goal": "depends on ghost",
            "capability": "code",
            "dependencies": ["m_does_not_exist"],
        },
    )

    assert response.status_code == 422
    assert "unknown mission" in response.json()["detail"].lower()


# ---------------------------------------------------------------
# E+F+G+H. cancellation/timeout during a long subprocess; no commit
# ---------------------------------------------------------------

def test_cancel_during_long_subprocess_no_commit(tmp_path):
    repo = Path(tmp_path) / "repo"
    baseline = _init_repo(repo, slow_test=True)

    coordinator, store = _make_coordinator(
        tmp_path, _real_worker()
    )

    store.enqueue(
        Mission(
            goal="change greet",
            capability="repo-code",
            metadata={
                "repo_path": str(repo),
                "target_file": "app.py",
                "find": "return 'old'",
                "replace": "return 'hello'",
            },
        )
    )
    mission_id = store.list()[0].id

    coordinator.start()
    started = time.monotonic()
    try:
        deadline = time.monotonic() + 30

        while time.monotonic() < deadline:
            events = store.events(mission_id)

            if any(
                e["event_type"] == "validation.started"
                for e in events
            ):
                break

            time.sleep(0.05)
        else:
            raise AssertionError("validation never started")

        # Cancellation lands while the long pytest subprocess is
        # running; the process group must be torn down and the
        # mission cancelled without any commit.
        store.request_cancel(mission_id)

        mission = _wait_terminal(store, mission_id, timeout=30)
    finally:
        coordinator.stop()

    assert time.monotonic() - started < 60
    assert mission.status == MissionStatus.cancelled
    assert _git(repo, "rev-parse", "HEAD") == baseline

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


def test_timeout_during_long_subprocess_no_commit(tmp_path):
    repo = Path(tmp_path) / "repo"
    baseline = _init_repo(repo, slow_test=True)

    coordinator, store = _make_coordinator(
        tmp_path, _real_worker()
    )

    store.enqueue(
        Mission(
            goal="change greet",
            capability="repo-code",
            metadata={
                "repo_path": str(repo),
                "target_file": "app.py",
                "find": "return 'old'",
                "replace": "return 'hello'",
                "timeout_seconds": 2,
            },
        )
    )
    mission_id = store.list()[0].id

    coordinator.start()
    started = time.monotonic()
    try:
        mission = _wait_terminal(store, mission_id, timeout=60)
    finally:
        coordinator.stop()

    assert time.monotonic() - started < 60
    assert mission.status != MissionStatus.passed
    error = mission.result.get("error") or {}
    assert error.get("type") == "Timeout"
    assert _git(repo, "rev-parse", "HEAD") == baseline


# ---------------------------------------------------------------
# J. secret blocks commit
# ---------------------------------------------------------------

def test_worker_secret_blocks_commit(tmp_path):
    repo = Path(tmp_path) / "repo"
    baseline = _init_repo(repo)

    worker = worker_module.RepoCodeWorker()
    result = worker.execute(
        "add a credential to the module",
        {
            "repo_path": str(repo),
            "target_file": "app.py",
            "find": "return 'old'",
            "replace": (
                "return 'hello'\n"
                "AWS_SECRET = 'AKIAIOSFODNN7EXAMPLE'\n"
            ),
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "SecretBlocked"
    assert result["output"]["secret_blocked"] is True

    scans = [
        e["report"]
        for e in result["evidence"]
        if isinstance(e, dict) and e.get("type") == "diff_scan"
    ]
    assert scans and scans[0]["blocking"]

    cleanup = [
        e["action"]
        for e in result["evidence"]
        if isinstance(e, dict) and e.get("type") == "worktree_cleanup"
    ]
    assert cleanup == ["kept_failed_for_debugging"]

    # Nothing was committed; the secret never reached the repository.
    assert _git(repo, "rev-parse", "HEAD") == baseline


# ---------------------------------------------------------------
# K. no-validation does not PASS
# ---------------------------------------------------------------

def test_no_validation_does_not_pass(tmp_path):
    repo = Path(tmp_path) / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "yodaw@test.local")
    _git(repo, "config", "user.name", "YODAW Test")
    (repo / "app.py").write_text("def greet():\n    return 'old'\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")

    worker = worker_module.RepoCodeWorker()
    result = worker.execute(
        "improve app",
        {
            "repo_path": str(repo),
            "target_file": "app.py",
            "find": "return 'old'",
            "replace": "return 'hello'",
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "NoTestsDetected"
    assert result["output"]["tests_passed"] is False
    assert _git(repo, "rev-parse", "HEAD") == baseline


# ---------------------------------------------------------------
# M. evidence_report on all terminal outcomes
# ---------------------------------------------------------------

def _evidence_report(result):
    report = result["output"]["evidence_report"]
    assert set(report) == EVIDENCE_KEYS
    return report


def test_evidence_report_pass():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        _init_repo(repo)

        worker = worker_module.RepoCodeWorker()
        result = worker.execute(
            "Modify app.py to say def greet():\n    return 'hello'\n",
            {"repo_path": str(repo)},
        )

        assert result["success"] is True
        report = _evidence_report(result)
        assert report["terminal"] == "PASS"
        assert report["commit_sha"]
        assert report["working_tree_clean"]


def test_evidence_report_fail():
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "repo"
        _init_repo(repo)

        worker = worker_module.RepoCodeWorker()
        result = worker.execute(
            "break greet",
            {
                "repo_path": str(repo),
                "target_file": "app.py",
                "find": "return 'old'",
                "replace": "return 'sideways'",
            },
        )

        assert result["success"] is False
        assert result["error"]["type"] == "ValidationFailed"
        report = _evidence_report(result)
        assert report["terminal"] == "FAIL"
        assert report["commit_sha"] is None


def test_evidence_report_blocked():
    import tempfile

    original = worker_module.generate_edit_plan

    def blocked_plan(goal, worktree, lessons=""):
        return {"action": "blocked", "reason": "insufficient info"}

    worker_module.generate_edit_plan = blocked_plan
    try:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _init_repo(repo)

            worker = worker_module.RepoCodeWorker()
            result = worker.execute(
                "do something complicated",
                {"repo_path": str(repo)},
            )
    finally:
        worker_module.generate_edit_plan = original

    assert result["success"] is False
    assert result["error"]["type"] == "LLMBlocked"
    report = _evidence_report(result)
    assert report["terminal"] == "BLOCKED"


def test_evidence_report_cancelled():
    import tempfile

    original = worker_module.generate_edit_plan

    def cancelling_plan(goal, worktree, lessons=""):
        raise MissionCancelled("probe cancel")

    worker_module.generate_edit_plan = cancelling_plan
    try:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _init_repo(repo)

            worker = worker_module.RepoCodeWorker()
            result = worker.execute(
                "do something complicated",
                {"repo_path": str(repo)},
            )
    finally:
        worker_module.generate_edit_plan = original

    assert result["success"] is False
    assert result["error"]["type"] == "Cancelled"
    report = _evidence_report(result)
    assert report["terminal"] == "CANCELLED"


def test_evidence_report_timeout():
    import tempfile

    original = worker_module.generate_edit_plan

    def timing_out_plan(goal, worktree, lessons=""):
        raise MissionTimeout("probe deadline")

    worker_module.generate_edit_plan = timing_out_plan
    try:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            _init_repo(repo)

            worker = worker_module.RepoCodeWorker()
            result = worker.execute(
                "do something complicated",
                {"repo_path": str(repo)},
            )
    finally:
        worker_module.generate_edit_plan = original

    assert result["success"] is False
    assert result["error"]["type"] == "Timeout"
    report = _evidence_report(result)
    assert report["terminal"] == "TIMEOUT"