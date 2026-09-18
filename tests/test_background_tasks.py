"""Background Task API tests.

These tests pin the invariants the architecture actually promises:

- the planner schema is strict and malformed planner output never
  continues (the task is BLOCKED, with the planner's own error kept)
- executor availability is real and selection falls back only to an
  executor that exists
- the lifecycle is durable, ordered, and restart-safe
- the event stream is reconnectable by sequence number
- cancellation and timeout stop real work
- ``COMPLETED`` requires real filesystem change, a real commit, and a
  verifier PASS — model text is never enough
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.background.engine import TaskEngine
from app.background.events import TaskEventBus
from app.background.executors.base import ExecutionContext, Executor
from app.background.executors.registry import ExecutorRegistry
from app.background.models import (
    PlanResult,
    PlannerError,
    TaskRequest,
    TaskState,
)
from app.background.planner import GrokArchitect, extract_json_object
from app.background.store import TaskStore
from app.background.verifier import TaskVerifier


# ------------------------------------------------------------- fixtures
def make_repo(root: Path, with_build: bool = False) -> Path:
    """A real disposable git repo with a real (failing) test."""
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n"
    )
    (repo / "sample.py").write_text("VALUE = 1\n")
    if with_build:
        (repo / "package.json").write_text(
            json.dumps({"name": "x", "scripts": {"build": "true"}})
        )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "k@example.com"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Kodgar"], cwd=repo, check=True
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


class StubArchitect:
    """A planner with a fixed, valid answer (engine-level tests)."""

    name = "grok"
    attempts: list = []

    def __init__(self, plan: PlanResult):
        self.plan_result = plan

    def plan(self, goal, repo, previous_evidence=None, cancel_check=None):
        if cancel_check:
            cancel_check()
        return self.plan_result, "stub", self.plan_result.reason


class BrokenArchitect:
    name = "grok"
    attempts = [type("A", (), {"backend": "stub", "error": "bad json"})()]

    def plan(self, goal, repo, previous_evidence=None, cancel_check=None):
        raise PlannerError("stub planner returned malformed JSON")


def create_plan(**overrides) -> PlanResult:
    payload = {
        "task_type": "file_creation",
        "summary": "create the marker file",
        "languages": ["text"],
        "frameworks": [],
        "tools": ["git"],
        "executor": "kodgar-native",
        "reason": "deterministic single file",
        "plan": ["create MARKER.txt"],
        "acceptance": ["marker file exists"],
        "target_files": ["MARKER.txt"],
        "edits": [
            {"target_file": "MARKER.txt", "find": "", "replace": "OK\n"}
        ],
    }
    payload.update(overrides)
    return PlanResult.model_validate(payload)


@pytest.fixture()
def store(tmp_path):
    return TaskStore(tmp_path / "tasks.db")


@pytest.fixture(autouse=True)
def _hermetic_health(monkeypatch):
    """Keep default registries hermetic: no live binary/inference probes.

    The default ExecutorRegistry uses the process-wide health service;
    swap it for a stub that derives health from ``available()`` only.
    Tests that specifically exercise health/circuit behavior construct
    their own ``ExecutorHealthService`` with fake probes.
    """
    import app.background.executors.registry as registry_mod
    from app.background.executors import health as health_mod
    from app.background.models import ExecutorHealth

    class _StubHealth:
        def health(self, executor, force=False):
            installed, detail = executor.available()
            return ExecutorHealth(
                id=executor.id,
                label=executor.label,
                kind=executor.kind,
                installed=installed,
                authenticated=True if installed else None,
                model_available=True if installed else None,
                inference_ok=True if installed else None,
                healthy=installed,
                eligible=installed,
                detail=detail,
                capabilities=list(getattr(executor, "capabilities", ()) or ()),
            )

        def record_failure(self, executor_id, message, status=None):
            return health_mod.classify_executor_error(message, status)

        def record_success(self, executor_id):
            pass

        def eligible_ids(self, executors):
            return [e.id for e in executors if self.health(e).eligible]

        def status(self, executors):
            hs = [self.health(e) for e in executors]
            return {
                "executors": {h.id: h.model_dump() for h in hs},
                "available": [h.id for h in hs if h.eligible],
                "unavailable": [h.id for h in hs if not h.eligible],
                "healthy": [h.id for h in hs if h.healthy],
            }

    stub = _StubHealth()
    monkeypatch.setattr(registry_mod, "_health_service", stub)
    yield stub


@pytest.fixture()
def engine_factory(store, tmp_path):
    engines: list[TaskEngine] = []

    def build(architect=None, **kwargs):
        kwargs.setdefault("worktree_root", tmp_path / "worktrees")
        engine = TaskEngine(
            store=store,
            bus=TaskEventBus(store),
            architect=architect or StubArchitect(create_plan()),
            **kwargs,
        )
        engines.append(engine)
        return engine

    yield build
    for engine in engines:
        engine.stop(timeout=5)


def wait_for_terminal(store, task_id, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = store.get(task_id)
        if task is not None and task.state in {
            TaskState.completed,
            TaskState.failed,
            TaskState.blocked,
            TaskState.cancelled,
        }:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish within {timeout}s")


# ------------------------------------------------------ planner schema
def test_plan_result_is_strict():
    with pytest.raises(Exception):
        PlanResult.model_validate(
            {
                "task_type": "x",
                "summary": "y",
                "languages": ["python"],
                "frameworks": [],
                "tools": [],
                "executor": "claude-code",
                "reason": "r",
                "plan": ["p"],
                "acceptance": ["a"],
                "surprise_extra_key": True,
            }
        )


def test_plan_requires_a_known_executor():
    with pytest.raises(Exception):
        PlanResult.model_validate(
            {
                "task_type": "x",
                "summary": "y",
                "languages": ["python"],
                "frameworks": [],
                "tools": [],
                "executor": "some-other-agent",
                "reason": "r",
                "plan": ["p"],
                "acceptance": ["a"],
            }
        )


def test_plan_rejects_path_escape_in_scope():
    with pytest.raises(Exception):
        create_plan(expected_scope=["../secrets.env"])


def test_extract_json_object_handles_fences_and_prose():
    assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    assert extract_json_object("no json here") is None


def test_extract_json_object_unwraps_cli_envelope():
    wrapped = json.dumps({"result": json.dumps({"executor": "codex", "x": 1})})
    assert extract_json_object(wrapped) == {"executor": "codex", "x": 1}


def test_architect_rejects_malformed_output():
    architect = GrokArchitect(backends=["local"], repo_intel_fn=lambda repo: {})
    with pytest.raises(PlannerError):
        architect._parse("{not json", "unit")
    with pytest.raises(PlannerError):
        architect._parse('{"executor": "codex"}', "unit")


def test_architect_reports_cli_error_envelope():
    architect = GrokArchitect(backends=["local"], repo_intel_fn=lambda repo: {})
    with pytest.raises(PlannerError) as excinfo:
        architect._parse('{"type": "error", "message": "403 forbidden"}', "unit")
    assert "403 forbidden" in str(excinfo.value)


def test_architect_fails_over_to_the_next_backend():
    architect = GrokArchitect(
        backends=["9router", "local"], repo_intel_fn=lambda repo: {}
    )
    good = json.dumps(
        {
            "task_type": "simple",
            "summary": "s",
            "languages": ["python"],
            "frameworks": [],
            "tools": [],
            "executor": "kodgar-native",
            "reason": "r",
            "plan": ["do it"],
            "acceptance": ["done"],
        }
    )
    architect._run_9router = lambda prompt: (_ for _ in ()).throw(
        PlannerError("route down")
    )
    architect._run_local = lambda prompt: good
    plan, backend, _ = architect.plan("do it", None)
    assert backend == "local"
    assert plan.executor == "kodgar-native"


def test_architect_raises_when_every_backend_fails():
    architect = GrokArchitect(backends=["9router"], repo_intel_fn=lambda repo: {})
    architect._run_9router = lambda prompt: (_ for _ in ()).throw(
        PlannerError("route down")
    )
    with pytest.raises(PlannerError):
        architect.plan("do it", None)


# --------------------------------------------------------- executors
def test_executor_registry_reports_real_availability():
    registry = ExecutorRegistry()
    infos = {info.id: info for info in registry.infos()}
    native = infos["kodgar-native"]
    assert native.available is True
    assert "codex" in infos
    # Availability mirrors the machine: if there is no binary, the
    # executor must say so with a reason.
    import shutil

    assert infos["codex"].available == bool(shutil.which("codex"))


def test_selection_prefers_the_planners_choice_when_available():
    registry = ExecutorRegistry()
    chosen, reason = registry.select(create_plan())
    assert chosen == "kodgar-native"
    assert "planner selected" in reason


class _Missing(Executor):
    id = "codex"
    label = "Codex"
    kind = "cli"

    def available(self):
        return False, "codex is not installed on this machine"

    def execute(self, ctx):  # pragma: no cover - never selected
        raise AssertionError("unavailable executor was selected")


class _Present(Executor):
    id = "kodgar-native"
    label = "Kodgar native"
    kind = "native"

    def available(self):
        return True, "built in"

    def execute(self, ctx):  # pragma: no cover - never selected here
        raise AssertionError("not executed in this test")


def test_selection_falls_back_when_the_choice_is_missing():
    registry = ExecutorRegistry(executors=[_Missing(), _Present()])
    chosen, reason = registry.select(create_plan(executor="codex"))
    assert chosen == "kodgar-native"
    assert "unavailable" in reason
    assert "not installed" in reason


def test_selection_refuses_when_nothing_is_available():
    registry = ExecutorRegistry(executors=[_Missing()])
    with pytest.raises(RuntimeError):
        registry.select(create_plan(executor="codex"))


def test_user_override_is_honoured_and_explained():
    registry = ExecutorRegistry()
    chosen, reason = registry.select(
        create_plan(), preference="claude-code"
    )
    if "claude-code" in registry.available_ids():
        assert chosen == "claude-code"
        assert "user override accepted" in reason
    else:
        assert "fell back" in reason


def test_native_executor_writes_real_files_in_the_worktree(tmp_path):
    from app.background.executors.kodgar_native import KodgarNativeExecutor

    repo = make_repo(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = ExecutionContext(
        task=_task(str(repo)),
        repo=repo,
        worktree=worktree,
        plan=create_plan(),
    )
    outcome = KodgarNativeExecutor().execute(ctx)
    assert outcome.succeeded is True
    assert (worktree / "MARKER.txt").read_text() == "OK\n"
    assert outcome.files_touched == ["MARKER.txt"]


def test_native_executor_rejects_a_path_escape(tmp_path):
    from app.background.executors.kodgar_native import KodgarNativeExecutor

    repo = make_repo(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    plan = create_plan(
        target_files=[],
        edits=[{"target_file": "../escaped.txt", "find": "", "replace": "x"}],
    )
    ctx = ExecutionContext(
        task=_task(str(repo)), repo=repo, worktree=worktree, plan=plan
    )
    outcome = KodgarNativeExecutor().execute(ctx)
    assert outcome.succeeded is False
    assert (tmp_path / "escaped.txt").exists() is False


def _task(repo: str):
    from app.background.models import TaskRecord

    return TaskRecord(goal="create marker", repo=repo)


# ---------------------------------------------------------- lifecycle
def test_full_lifecycle_reaches_completed_with_real_commit(
    engine_factory, store, tmp_path
):
    repo = make_repo(tmp_path)
    engine = engine_factory()
    engine.start()
    task = engine.submit(
        TaskRequest(goal="create MARKER.txt", repo=str(repo))
    )
    final = wait_for_terminal(store, task.id)

    assert final.state is TaskState.completed
    assert final.commit_sha
    assert final.verify_status == "PASS"
    assert final.progress == 100
    assert (repo / "MARKER.txt").exists() is False  # user branch untouched

    types = [event.type for event in store.events(task.id)]
    for expected in (
        "task.created",
        "planner.started",
        "planner.completed",
        "executor.selected",
        "executor.started",
        "file.changed",
        "verification.started",
        "verification.completed",
        "git.committed",
        "task.completed",
    ):
        assert expected in types, f"missing event {expected} in {types}"

    # The commit really contains the work and really landed on the
    # isolated branch.
    show = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:", final.commit_sha],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "MARKER.txt" in show.stdout


def test_state_progress_is_derived_from_stages(engine_factory, store, tmp_path):
    repo = make_repo(tmp_path)
    engine = engine_factory()
    engine.start()
    task = engine.submit(TaskRequest(goal="create MARKER.txt", repo=str(repo)))
    seen: dict[str, int] = {}
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        for event in store.events(task.id):
            if event.type == "task.state":
                seen[event.data["state"]] = event.data["progress"]
        current = store.get(task.id)
        if current.state in {
            TaskState.completed,
            TaskState.failed,
            TaskState.blocked,
        }:
            break
        time.sleep(0.05)

    assert seen["PLANNING"] == 5
    assert seen["PREPARING"] == 28
    assert seen["CODING"] == 40
    assert seen["TESTING"] == 70
    assert seen["VERIFYING"] == 84
    assert seen["COMMITTING"] == 94
    assert seen["COMPLETED"] == 100


def test_planner_failure_blocks_instead_of_running(engine_factory, store, tmp_path):
    repo = make_repo(tmp_path)
    engine = engine_factory(architect=BrokenArchitect())
    engine.start()
    task = engine.submit(TaskRequest(goal="anything", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.blocked
    assert final.error["type"] == "PlannerError"
    # Nothing was executed: no worktree, no commit, no executor.
    assert final.commit_sha is None
    assert final.executor is None


def test_task_rejects_a_non_repository(engine_factory, tmp_path):
    engine = engine_factory()
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(ValueError):
        engine.submit(TaskRequest(goal="x", repo=str(not_a_repo)))


def test_task_requires_a_repo(engine_factory):
    engine = engine_factory()
    with pytest.raises(ValueError):
        engine.submit(TaskRequest(goal="x"))


def test_cancellation_stops_a_running_task(engine_factory, store, tmp_path):
    repo = make_repo(tmp_path)

    class SlowArchitect(StubArchitect):
        def plan(self, goal, repo, previous_evidence=None, cancel_check=None):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if cancel_check:
                    cancel_check()
                time.sleep(0.05)
            return super().plan(goal, repo, previous_evidence, cancel_check)

    engine = engine_factory(architect=SlowArchitect(create_plan()))
    engine.start()
    task = engine.submit(TaskRequest(goal="create MARKER.txt", repo=str(repo)))
    time.sleep(0.5)
    outcome = engine.cancel(task.id)
    assert outcome in {"cancelled", "requested"}
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.cancelled
    assert final.commit_sha is None


def test_timeout_fails_a_task(engine_factory, store, tmp_path):
    repo = make_repo(tmp_path)

    class SlowArchitect(StubArchitect):
        def plan(self, goal, repo, previous_evidence=None, cancel_check=None):
            time.sleep(5)
            return super().plan(goal, repo, previous_evidence, cancel_check)

    engine = engine_factory(architect=SlowArchitect(create_plan()), timeout_seconds=1)
    engine.start()
    task = engine.submit(TaskRequest(goal="create MARKER.txt", repo=str(repo)))
    assert task.timeout_seconds == 1
    final = wait_for_terminal(store, task.id, timeout=30)
    assert final.state is TaskState.failed
    assert final.error["type"] == "Timeout"


# ------------------------------------------------------------- storage
def test_state_survives_a_restart(store, tmp_path):
    repo = make_repo(tmp_path)
    engine = TaskEngine(
        store=store,
        bus=TaskEventBus(store),
        architect=StubArchitect(create_plan()),
        worktree_root=tmp_path / "worktrees",
    )
    engine.start()
    task = engine.submit(TaskRequest(goal="create MARKER.txt", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    engine.stop(timeout=5)

    reopened = TaskStore(store.db_path)
    restored = reopened.get(task.id)
    assert restored is not None
    assert restored.state is final.state
    assert restored.commit_sha == final.commit_sha
    assert reopened.max_seq(task.id) == store.max_seq(task.id)


def test_recovery_requeues_non_terminal_tasks(store, tmp_path):
    repo = make_repo(tmp_path)
    task = _task(str(repo))
    store.create(task)
    store.transition(task.id, TaskState.coding, "coding", progress=40)
    recovered = store.recover_interrupted()
    assert task.id in recovered
    restored = store.get(task.id)
    assert restored.state is TaskState.queued
    assert restored.cancel_requested is False


def test_event_sequence_is_ordered_and_monotonic(store):
    task = _task("/tmp/whatever")
    store.create(task)
    for index in range(5):
        store.append_event(task.id, "test.event", {"index": index})
    events = store.events(task.id)
    assert [event.seq for event in events] == sorted(event.seq for event in events)
    assert [event.data["index"] for event in events] == [0, 1, 2, 3, 4]


def test_logs_are_bounded_and_ordered(store):
    task = _task("/tmp/whatever")
    store.create(task)
    store.append_log(task.id, [f"line {i}" for i in range(50)])
    logs = store.logs(task.id, limit=10)
    assert len(logs) == 10
    assert logs[-1]["line"] == "line 49"


# -------------------------------------------------------- event stream
def test_sse_reconnect_replays_by_sequence(store, tmp_path):
    repo = make_repo(tmp_path)
    bus = TaskEventBus(store)
    engine = TaskEngine(
        store=store,
        bus=bus,
        architect=StubArchitect(create_plan()),
        worktree_root=tmp_path / "worktrees",
    )
    engine.start()
    try:
        task = engine.submit(TaskRequest(goal="create MARKER.txt", repo=str(repo)))
        wait_for_terminal(store, task.id)

        first_pass = list(bus.stream(task.id, after_seq=0))
        assert first_pass, "stream produced no events"
        # Simulate a browser that saw the first three events, then
        # dropped the connection and reconnected with its last id.
        cut = first_pass[2].seq
        second_pass = list(bus.stream(task.id, after_seq=cut))
        assert [event.seq for event in second_pass] == [
            event.seq for event in first_pass[3:]
        ]
        assert all(event.seq > cut for event in second_pass)
    finally:
        engine.stop(timeout=5)


def test_sse_frames_carry_id_and_type(store):
    task = _task("/tmp/whatever")
    store.create(task)
    event = store.append_event(task.id, "task.created", {"goal": "x"})
    from app.background.events import format_event

    frame = format_event(event)
    assert f"id: {event.seq}" in frame
    assert "event: task.created" in frame
    assert '"task_id"' in frame


# ----------------------------------------------------------- verifier
def test_verifier_refuses_pass_without_real_change(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: nowhere\n")
    verifier = TaskVerifier()
    report = verifier.verify_worktree(
        _task(str(repo)), create_plan(), repo, worktree, suite=None
    )
    assert report.status == "FAIL"
    assert any(
        check.name == "files_changed" and check.status == "FAIL"
        for check in report.checks
    )


def test_verifier_requires_exact_content(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=worktree, check=True
    )
    subprocess.run(["git", "config", "user.email", "k@e.com"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "k"], cwd=worktree, check=True)
    (worktree / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=worktree, check=True)

    (worktree / "MARKER.txt").write_text("WRONG\n")
    report = TaskVerifier().verify_worktree(
        _task(str(repo)), create_plan(), repo, worktree, suite=None
    )
    content = [c for c in report.checks if c.name == "exact_content"][0]
    assert content.status == "FAIL"
    assert report.status == "FAIL"


def test_verifier_rejects_out_of_scope_changes(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "k@e.com"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "k"], cwd=worktree, check=True)
    (worktree / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=worktree, check=True)

    (worktree / "MARKER.txt").write_text("OK\n")
    (worktree / "unrelated.txt").write_text("surprise\n")
    report = TaskVerifier().verify_worktree(
        _task(str(repo)), create_plan(), repo, worktree, suite=None
    )
    scope = [c for c in report.checks if c.name == "scope"][0]
    assert scope.status == "FAIL"
    assert report.out_of_scope == ["unrelated.txt"]


def test_commit_check_requires_the_commit_to_exist(tmp_path):
    repo = make_repo(tmp_path)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "k@e.com"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "k"], cwd=worktree, check=True)
    (worktree / "MARKER.txt").write_text("OK\n")
    subprocess.run(["git", "add", "-A"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "do it"], cwd=worktree, check=True)

    verifier = TaskVerifier()
    missing = verifier.verify_commit(
        _task(str(repo)), create_plan(), repo, worktree, None, ["MARKER.txt"]
    )
    assert missing.status == "FAIL"

    sha = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    ok = verifier.verify_commit(
        _task(str(repo)), create_plan(), repo, worktree, sha, ["MARKER.txt"]
    )
    assert ok.status == "PASS"

    wrong_files = verifier.verify_commit(
        _task(str(repo)), create_plan(), repo, worktree, sha, ["NOPE.txt"]
    )
    assert wrong_files.status == "FAIL"


def test_engine_reports_failure_when_nothing_changed(engine_factory, store, tmp_path):
    """An executor that returns success without touching disk must fail.

    This is the "no fake PASS" guarantee: a clean exit code plus a
    confident summary is not evidence of anything.
    """
    from app.background.models import ExecutionOutcome

    class LyingExecutor(Executor):
        id = "kodgar-native"
        label = "Kodgar native"
        kind = "native"

        def available(self):
            return True, "test double"

        def execute(self, ctx):
            ctx.on_output("kodgar", "I have implemented the task successfully")
            return ExecutionOutcome(
                succeeded=True, exit_code=0, summary="all done, trust me"
            )

    repo = make_repo(tmp_path)
    engine = engine_factory(
        architect=StubArchitect(create_plan()),
    )
    engine.registry = ExecutorRegistry(executors=[LyingExecutor()])
    engine.start()
    task = engine.submit(TaskRequest(goal="do nothing", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.failed
    assert final.commit_sha is None
    assert final.verify_status == "FAIL"
    assert final.error["type"] == "VerificationFailed"
    failed = [c.name for c in final.verification.checks if c.status == "FAIL"]
    assert "files_changed" in failed


def test_engine_reports_verification_failure_when_work_is_wrong(
    engine_factory, store, tmp_path
):
    repo = make_repo(tmp_path)
    plan = create_plan(
        edits=[
            {"target_file": "MARKER.txt", "find": "", "replace": "EXACT\n"}
        ],
        acceptance=["marker exists"],
    )

    class WrongExecutor(Executor):
        id = "kodgar-native"
        label = "Kodgar native"
        kind = "native"

        def available(self):
            return True, "test double"

        def execute(self, ctx):
            (ctx.worktree / "MARKER.txt").write_text("NOT THE EXACT CONTENT\n")
            from app.background.models import ExecutionOutcome

            return ExecutionOutcome(succeeded=True, exit_code=0, summary="lied")

    engine = engine_factory(architect=StubArchitect(plan))
    engine.registry = ExecutorRegistry(executors=[WrongExecutor()])
    engine.start()
    task = engine.submit(TaskRequest(goal="create marker", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.failed
    assert final.commit_sha is None
    failed = [c.name for c in final.verification.checks if c.status == "FAIL"]
    assert "exact_content" in failed


# ------------------------------------------------------------- API
def test_task_api_contract(tmp_path, monkeypatch):
    os.environ["KODGAR_TASKS_DB"] = str(tmp_path / "api_tasks.db")
    from app.background import service

    service.reset_for_tests()
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        repo = make_repo(tmp_path)
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/tasks",
                json={"goal": "create MARKER.txt", "repo": str(repo)},
            )
            assert response.status_code == 202
            body = response.json()
            assert body["state"] == "QUEUED"
            task_id = body["task_id"]

            detail = client.get(f"/api/v1/tasks/{task_id}")
            assert detail.status_code == 200
            payload = detail.json()
            for key in (
                "id",
                "state",
                "progress",
                "current_step",
                "executor",
                "planner",
                "started_at",
                "elapsed_seconds",
                "files_changed",
                "tests",
            ):
                assert key in payload

            assert client.get("/api/v1/tasks").status_code == 200
            assert client.get("/api/v1/executors").status_code == 200
            assert (
                client.get("/api/v1/tasks/t_does_not_exist").status_code == 404
            )
            assert client.get("/api/v1/tasks/..%2Fetc").status_code in (404, 422)
    finally:
        service.reset_for_tests()
        os.environ.pop("KODGAR_TASKS_DB", None)


def test_task_api_rejects_an_unknown_repo(tmp_path, monkeypatch):
    os.environ["KODGAR_TASKS_DB"] = str(tmp_path / "api_tasks2.db")
    from app.background import service

    service.reset_for_tests()
    try:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/tasks",
                json={"goal": "x", "repo": str(tmp_path / "nope")},
            )
            assert response.status_code == 422
    finally:
        service.reset_for_tests()
        os.environ.pop("KODGAR_TASKS_DB", None)
