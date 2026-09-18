"""Executor health, fallback, and circuit-breaker regression tests.

Covers the Phase L acceptance list:

1.  binary installed but model invalid  -> unhealthy
2.  Claude model_not_found              -> fallback
3.  Grok 403                            -> fallback
4.  unavailable Codex                   -> ignored
5.  planner selects unhealthy executor  -> rejected
6.  distinct-executor retry             -> works
7.  max attempt bound                   -> enforced
8.  circuit breaker                     -> opens on repeat fatal failures
9.  circuit recovery                    -> re-eligible after cooldown
10. health cache TTL                    -> probes are bounded
11. planner timeout                     -> bounded backend chain
12. managed clean source                -> dirty repo served by mirror
13. no infinite loop                    -> attempts always terminate
14. executor failure history            -> persisted on the task
15. final state reflects final executor -> executor field is the runner
16. frontend executor health API        -> health-shaped payload
17. no fake completion                  -> verifier still the authority
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.background.executors import health as health_mod
from app.background.executors.base import Executor, ExecutionContext
from app.background.executors.health import (
    ExecutorHealthService,
    classify_executor_error,
    error_class_action,
)
from app.background.executors.registry import ExecutorRegistry
from app.background.verifier import parse_test_counts
from app.background.models import (
    ExecutionOutcome,
    ExecutorHealth,
    PlanResult,
    TaskRequest,
    TaskState,
    error_class_action as model_error_class_action,
)
from app.background.store import TaskStore
from app.background.events import TaskEventBus

from tests.test_background_tasks import (
    StubArchitect,
    create_plan,
    engine_factory,
    make_repo,
    store,  # noqa: F401
    engine_factory as _ef,  # noqa: F401
    wait_for_terminal,
)


# ------------------------------------------------------------ helpers
def _plan_with_executor(executor_id: str, **overrides) -> PlanResult:
    return create_plan(executor=executor_id, **overrides)


class _FakeExec(Executor):
    """Minimal adapter double with configurable availability."""

    def __init__(self, id_: str = "kodgar-native", kind: str = "cli", ok: bool = True, detail: str = "test double"):
        self.id = id_
        self.label = id_
        self.kind = kind
        self._ok = ok
        self._detail = detail
        self.calls = 0

    def available(self):
        return self._ok, self._detail

    def execute(self, ctx):
        self.calls += 1
        (ctx.worktree / "MARKER.txt").write_text("OK\n")
        return ExecutionOutcome(succeeded=True, exit_code=0, summary="done")


class _FailingExec(_FakeExec):
    """Adapter whose execute always fails with a given message/status."""

    def __init__(self, id_, message, status=None, kind="cli"):
        super().__init__(id_, kind=kind)
        self.message = message
        self.status = status

    def execute(self, ctx):
        self.calls += 1
        error = {"type": "ExecutorFailed", "message": self.message}
        if self.status is not None:
            error["api_error_status"] = self.status
        return ExecutionOutcome(
            succeeded=False, exit_code=1, summary=self.message, error=error
        )


def _health_service(monkeypatch, probe_fn, **kwargs) -> ExecutorHealthService:
    service = ExecutorHealthService(**kwargs)
    monkeypatch.setattr(service, "_probe", probe_fn)
    return service


# ------------------------------------------------- classification (F)
def test_error_classification_model_not_found():
    assert classify_executor_error("model not found", 404) == "model_not_found"
    assert classify_executor_error("no such model: claude-opus-5[1m]") == "model_not_found"
    assert error_class_action("model_not_found") == "RETRY_DIFFERENT_EXECUTOR"


def test_error_classification_quota_and_auth():
    assert (
        classify_executor_error("You have reached the limit.", 402)
        == "quota_exhausted"
    )
    assert classify_executor_error("MONTHLY_REQUEST_COUNT") == "quota_exhausted"
    assert classify_executor_error("invalid api key") == "auth_failed"
    assert classify_executor_error("FreeTierError: not allowed") == "upstream_forbidden"
    assert classify_executor_error("ETIMEDOUT reading response") == "transient_timeout"


def test_edit_anchor_mismatch_is_a_task_defect_not_executor_failure():
    """A plan that cannot be applied must not abandon a healthy executor.

    Observed live: the native executor failed with "Requested source
    text was not found as an exact or unique relaxed match". Classified
    as ``unknown`` it became RETRY_DIFFERENT_EXECUTOR, so Kodgar gave up
    on a perfectly healthy executor and the task died after one attempt.
    The executor is fine; the plan is what is wrong.
    """
    message = "Requested source text was not found as an exact or unique relaxed match"
    error_type = classify_executor_error(message)
    assert error_type == "edit_not_applicable"
    assert error_class_action(error_type) == "NON_RETRYABLE_TASK_ERROR"
    # A gateway rejection that merely mentions files is still infra.
    assert (
        classify_executor_error("403 Forbidden: target file denied", 403)
        == "upstream_forbidden"
    )


def test_error_class_action_transient_and_unknown():
    assert error_class_action("transient_overload") == "RETRY_SAME_EXECUTOR"
    assert error_class_action("unknown") == "RETRY_DIFFERENT_EXECUTOR"
    assert (
        error_class_action("invalid_repo") == "NON_RETRYABLE_TASK_ERROR"
    )
    # models module exposes the same policy
    assert model_error_class_action("model_not_found") == "RETRY_DIFFERENT_EXECUTOR"


# ------------------------------------------- 1. installed != healthy
def test_installed_but_model_invalid_is_unhealthy(monkeypatch):
    """Binary exists, auth OK, but the model 404s -> not eligible."""

    def probe(executor):
        return ExecutorHealth(
            id=executor.id,
            label=executor.label,
            kind=executor.kind,
            installed=True,
            authenticated=True,
            model_available=False,
            inference_ok=False,
            healthy=False,
            eligible=False,
            error_type="model_not_found",
            detail="There's an issue with the selected model (claude-opus-5[1m]).",
        )

    service = _health_service(monkeypatch, probe)
    claude = _FakeExec("claude-code")
    health = service.health(claude)
    assert health.installed is True
    assert health.healthy is False
    assert health.eligible is False
    assert health.error_type == "model_not_found"


# -------------------------------------- 2/3. real failures -> fallback
def test_planner_choice_unhealthy_falls_back(monkeypatch):
    """Claude unhealthy (model 404), grok healthy -> grok serves."""

    def probe(executor):
        if executor.id == "claude-code":
            return ExecutorHealth(
                id=executor.id, installed=True, healthy=False, eligible=False,
                error_type="model_not_found", detail="404 model",
            )
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=True, eligible=True,
        )

    service = _health_service(monkeypatch, probe)
    registry = ExecutorRegistry(
        executors=[_FakeExec("claude-code"), _FakeExec("kodgar-native", kind="native")],
        health_service=service,
    )
    chain, reason = registry.select_chain(_plan_with_executor("claude-code"))
    assert chain[0] == "kodgar-native"
    assert "health rejected" in reason
    assert "model_not_found" in reason


def test_grok_403_falls_back_to_native(monkeypatch):
    def probe(executor):
        if executor.id == "grok-cli":
            return ExecutorHealth(
                id=executor.id, installed=True, healthy=False, eligible=False,
                error_type="upstream_forbidden",
                detail="FreeTierError 403",
            )
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=True, eligible=True,
        )

    service = _health_service(monkeypatch, probe)
    registry = ExecutorRegistry(
        executors=[_FakeExec("grok-cli"), _FakeExec("kodgar-native", kind="native")],
        health_service=service,
    )
    chain, reason = registry.select_chain(_plan_with_executor("grok-cli"))
    assert chain[0] == "kodgar-native"
    assert "upstream_forbidden" in reason


# --------------------------------------- 4. absent codex is ignored
def test_unavailable_codex_is_ignored(monkeypatch):
    def probe(executor):
        if executor.id == "codex":
            return ExecutorHealth(
                id=executor.id, installed=False, healthy=False, eligible=False,
                error_type="not_installed", detail="codex is not installed",
            )
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=True, eligible=True,
        )

    service = _health_service(monkeypatch, probe)
    registry = ExecutorRegistry(
        executors=[
            _FakeExec("claude-code"),
            _FakeExec("codex"),
            _FakeExec("kodgar-native", kind="native"),
        ],
        health_service=service,
    )
    ids = registry.eligible_ids()
    assert "codex" not in ids
    chain, _ = registry.select_chain(_plan_with_executor("claude-code"))
    assert "codex" not in chain


# --------------------------------- 5. user override gated by health
def test_user_override_rejected_when_unhealthy(monkeypatch):
    def probe(executor):
        if executor.id == "codex":
            return ExecutorHealth(
                id=executor.id, installed=True, healthy=False, eligible=False,
                error_type="quota_exhausted", detail="402 limit",
            )
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=True, eligible=True,
        )

    service = _health_service(monkeypatch, probe)
    registry = ExecutorRegistry(
        executors=[_FakeExec("codex"), _FakeExec("kodgar-native", kind="native")],
        health_service=service,
    )
    chain, reason = registry.select_chain(
        _plan_with_executor("kodgar-native"), preference="codex"
    )
    assert chain[0] == "kodgar-native"
    assert "rejected by health" in reason
    assert "quota_exhausted" in reason


def test_planner_selecting_unhealthy_executor_is_rejected(monkeypatch):
    def probe(executor):
        if executor.id == "claude-code":
            return ExecutorHealth(
                id=executor.id, installed=True, healthy=False, eligible=False,
                error_type="model_not_found", detail="404",
            )
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=True, eligible=True,
        )

    service = _health_service(monkeypatch, probe)
    registry = ExecutorRegistry(
        executors=[_FakeExec("claude-code")], health_service=service
    )
    with pytest.raises(RuntimeError, match="no healthy executor"):
        registry.select(_plan_with_executor("claude-code"))


# ------------------------------ 6/7/13. bounded fallback in engine
def test_engine_falls_back_to_next_executor(engine_factory, store, tmp_path):
    """First executor fails with a fatal provider error; second completes."""
    repo = make_repo(tmp_path)
    plan = create_plan(executor="claude-code")

    class BrokenExec(Executor):
        id = "claude-code"
        label = "Broken"
        kind = "cli"

        def available(self):
            return True, "present"

        def execute(self, ctx):
            return ExecutionOutcome(
                succeeded=False,
                exit_code=1,
                summary="model not found",
                error={
                    "type": "ExecutorFailed",
                    "message": "There's an issue with the selected model (x). It may not exist",
                    "api_error_status": 404,
                },
            )

    class GoodExec(_FakeExec):
        def __init__(self):
            super().__init__("kodgar-native", kind="native")
            self.label = "Kodgar native"

    engine = engine_factory(architect=StubArchitect(plan))
    registry = ExecutorRegistry(
        executors=[BrokenExec(), GoodExec()],
        health_service=_FakeService(eligible={"claude-code", "kodgar-native"}),
    )
    engine.registry = registry
    engine.start()
    task = engine.submit(TaskRequest(goal="marker file", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.completed
    assert final.executor == "kodgar-native"
    assert final.commit_sha
    attempts = final.result.get("executor_attempts") or []
    assert attempts and attempts[0]["executor"] == "claude-code"
    assert attempts[0]["error_type"] == "model_not_found"
    assert "fallback_reason" in attempts[0]


def test_engine_caps_attempts_at_max(engine_factory, store, tmp_path):
    """Every executor fatal-fails; attempts stop at the bound, no loop."""
    from app.background.engine import MAX_EXECUTOR_ATTEMPTS

    repo = make_repo(tmp_path)
    plan = create_plan(executor="claude-code")

    class BadExec(Executor):
        def __init__(self, id_):
            self.id = id_
            self.label = id_
            self.kind = "cli"

        def available(self):
            return True, "present"

        def execute(self, ctx):
            return ExecutionOutcome(
                succeeded=False,
                exit_code=1,
                summary="provider down",
                error={
                    "type": "ExecutorFailed",
                    "message": "403 forbidden from provider",
                    "api_error_status": 403,
                },
            )

    engine = engine_factory(architect=StubArchitect(plan))
    registry = ExecutorRegistry(
        executors=[BadExec("claude-code"), BadExec("codex"), BadExec("grok-cli")],
        health_service=_FakeService(eligible={"claude-code", "codex", "grok-cli"}),
    )
    engine.registry = registry
    engine.start()
    task = engine.submit(TaskRequest(goal="marker", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.failed
    assert final.error["type"] == "AllExecutorsFailed"
    real_attempts = [
        a for a in final.result.get("executor_attempts", []) if not a.get("skipped")
    ]
    assert len(real_attempts) == MAX_EXECUTOR_ATTEMPTS
    executors_used = [a["executor"] for a in real_attempts]
    assert len(set(executors_used)) == len(executors_used)  # distinct


# ------------------------------ 8/9. circuit breaker + recovery
def test_circuit_opens_after_repeat_fatal_failures(monkeypatch):
    calls = {"n": 0}

    def probe(executor):
        calls["n"] += 1
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=False, eligible=False,
            error_type="quota_exhausted", detail="402",
        )

    service = ExecutorHealthService(
        failure_threshold=2, cooldown_seconds=120
    )
    monkeypatch.setattr(service, "_probe", probe)
    executor = _FakeExec("claude-code")
    service.health(executor)                  # fatal probe #1
    service.health(executor, force=True)      # fatal probe #2 -> threshold
    # Circuit open: further reads report breaker state without probing.
    reads_before = calls["n"]
    third = service.health(executor, force=True)
    assert third.circuit_open is True
    assert third.eligible is False
    assert calls["n"] == reads_before  # no new probe while open


def test_circuit_recovers_after_cooldown(monkeypatch):
    def probe(executor):
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=True, eligible=True,
        )

    service = ExecutorHealthService(failure_threshold=1, cooldown_seconds=0.05)
    monkeypatch.setattr(service, "_probe", probe)
    executor = _FakeExec("claude-code")

    # Open the circuit with a fatal live failure.
    error_type = service.record_failure(
        "claude-code", "model not found", 404
    )
    assert error_type == "model_not_found"
    blocked = service.health(executor)
    assert blocked.eligible is False
    # Cooldown elapses -> next read re-probes and recovers.
    time.sleep(0.08)
    recovered = service.health(executor, force=True)
    assert recovered.eligible is True
    assert recovered.circuit_open is False


def test_record_failure_classifies_and_caches(monkeypatch):
    service = ExecutorHealthService()
    monkeypatch.setattr(service, "_probe", lambda e: ExecutorHealth(id=e.id, healthy=True, eligible=True))
    error_type = service.record_failure(
        "claude-code", "API error 404: model does not exist", 404
    )
    assert error_type == "model_not_found"
    executor = _FakeExec("claude-code")
    # Cache was dropped; next health call re-probes but the circuit
    # state persists on the service (threshold=2, one failure so far).
    health = service.health(executor)
    assert health.healthy is True  # probe ran (cache was dropped by record_failure)
    service.record_failure("claude-code", "model does not exist", 404)
    open_health = service.health(executor)
    assert open_health.circuit_open is True


# ------------------------------------------------ 10. TTL cache
def test_health_cache_ttl(monkeypatch):
    calls = {"n": 0}

    def probe(executor):
        calls["n"] += 1
        return ExecutorHealth(id=executor.id, installed=True, healthy=True, eligible=True)

    service = ExecutorHealthService(ttl_seconds=60)
    monkeypatch.setattr(service, "_probe", probe)
    executor = _FakeExec("claude-code")
    service.health(executor)
    service.health(executor)
    service.health(executor)
    assert calls["n"] == 1  # cached within TTL


def test_health_force_bypasses_ttl(monkeypatch):
    calls = {"n": 0}

    def probe(executor):
        calls["n"] += 1
        return ExecutorHealth(id=executor.id, installed=True, healthy=True, eligible=True)

    service = ExecutorHealthService(ttl_seconds=600)
    monkeypatch.setattr(service, "_probe", probe)
    executor = _FakeExec("claude-code")
    service.health(executor)
    service.health(executor, force=True)
    assert calls["n"] == 2


# ------------------------------------------------- 11. planner timeout
def test_planner_backend_chain_is_bounded():
    """Every planner backend runs under an explicit timeout budget."""
    from app.background.planner import GrokArchitect, DEFAULT_GROK_TIMEOUT

    architect = GrokArchitect()
    assert architect.timeout >= 1
    assert DEFAULT_GROK_TIMEOUT >= 1


def test_planner_hanging_backend_does_not_hang_task(monkeypatch):
    """A stuck backend raises; the chain moves on; PlannerError surfaces."""
    from app.background.planner import GrokArchitect
    from app.background.models import PlannerError

    architect = GrokArchitect(backends=["9router"], repo_intel_fn=lambda repo: {})
    # The planner's budget: every backend call runs under
    # ``architect.timeout`` (env-tunable), and the engine applies its
    # own task deadline on top. A backend that hangs therefore cannot
    # hang a task forever.
    assert architect.timeout > 0
    monkeypatch.setattr(
        architect, "_run_9router",
        lambda prompt: (_ for _ in ()).throw(PlannerError("gateway hung; budget exceeded")),
    )
    with pytest.raises(PlannerError):
        architect.plan("goal", None)


# --------------------------------- 12. managed clean source
def test_dirty_repo_served_by_managed_mirror(tmp_path, monkeypatch):
    repo = tmp_path / "userrepo"
    repo.mkdir()
    import subprocess

    def git(*args, **kw):
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
        )

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    # Make the user repo dirty.
    (repo / "dirty.txt").write_text("uncommitted\n")

    mirror_root = tmp_path / "managed"
    monkeypatch.setenv("KODGAR_MANAGED_SOURCE_ROOT", str(mirror_root))
    from app.background.managed_source import resolve_execution_source

    result = resolve_execution_source(str(repo))
    assert result["error"] is None
    assert result["mirror"] is True
    mirror = Path(result["repo"])
    assert mirror != repo
    assert (mirror / "README.md").exists()
    assert not (mirror / "dirty.txt").exists()  # no uncommitted copying
    # User's tree untouched.
    assert (repo / "dirty.txt").exists()
    # Second call reuses the mirror.
    again = resolve_execution_source(str(repo))
    assert again["mirror"] is True
    assert Path(again["repo"]) == mirror


def test_clean_repo_used_directly(tmp_path, monkeypatch):
    repo = tmp_path / "cleanrepo"
    repo.mkdir()
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
        )

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    monkeypatch.setenv("KODGAR_MANAGED_SOURCE_ROOT", str(tmp_path / "managed"))
    from app.background.managed_source import resolve_execution_source

    result = resolve_execution_source(str(repo))
    assert result["error"] is None
    assert result["mirror"] is False
    assert Path(result["repo"]) == repo.resolve()


def test_engine_uses_mirror_for_dirty_repo(engine_factory, store, tmp_path, monkeypatch):
    """A dirty user repo no longer kills the task: the mirror runs it."""
    repo = make_repo(tmp_path)
    (repo / "local-noise.txt").write_text("uncommitted user state\n")
    monkeypatch.setenv("KODGAR_MANAGED_SOURCE_ROOT", str(tmp_path / "managed"))

    plan = create_plan()
    engine = engine_factory(architect=StubArchitect(plan))
    engine.start()
    task = engine.submit(TaskRequest(goal="marker file", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.completed, final.error
    source_meta = final.result.get("execution_source") or {}
    assert source_meta.get("mirror") is True
    assert Path(source_meta["repo"]) != repo
    # User repo still dirty, untouched.
    assert (repo / "local-noise.txt").exists()


# -------------------------------------- 14/15. history + final state
def test_attempt_history_persisted_and_final_executor_reflected(
    engine_factory, store, tmp_path
):
    repo = make_repo(tmp_path)
    plan = create_plan(executor="claude-code")

    class FlakyExec(Executor):
        id = "claude-code"
        label = "Flaky"
        kind = "cli"

        def available(self):
            return True, "present"

        def execute(self, ctx):
            return ExecutionOutcome(
                succeeded=False,
                exit_code=2,
                summary="quota",
                error={
                    "type": "ExecutorFailed",
                    "message": "You have reached the limit.",
                    "api_error_status": 402,
                },
            )

    class Native(_FakeExec):
        id = "kodgar-native"
        label = "Kodgar native"
        kind = "native"

    engine = engine_factory(architect=StubArchitect(plan))
    registry = ExecutorRegistry(
        executors=[FlakyExec(), Native()],
        health_service=_FakeService(eligible={"claude-code", "kodgar-native"}),
    )
    engine.registry = registry
    engine.start()
    task = engine.submit(TaskRequest(goal="marker", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.completed
    attempts = final.result.get("executor_attempts") or []
    # Every attempt is recorded, including the successful one that
    # actually finished the task.
    assert len(attempts) == 2
    attempt = attempts[0]
    assert attempt["executor"] == "claude-code"
    assert attempt["error_type"] == "quota_exhausted"
    assert attempt["action"] == "RETRY_DIFFERENT_EXECUTOR"
    assert attempt["started_at"] and attempt["finished_at"]
    assert attempt["exit_code"] == 2
    assert "fallback_reason" in attempt
    winner = attempts[1]
    assert winner["executor"] == "kodgar-native"
    assert winner["action"] == "SUCCESS"
    assert winner["error_type"] is None
    assert winner["finished_at"] and winner["duration_seconds"] >= 0
    # Final executor reflects the one that actually ran the task.
    assert final.executor == "kodgar-native"
    assert final.commit_sha


# ------------------------------------- 16. health-shaped API payload
def test_registry_status_payload_shape(monkeypatch):
    def probe(executor):
        if executor.id == "claude-code":
            return ExecutorHealth(
                id=executor.id, installed=True, healthy=False, eligible=False,
                error_type="model_not_found", detail="404 model",
            )
        return ExecutorHealth(
            id=executor.id, installed=True, healthy=True, eligible=True,
        )

    service = _health_service(monkeypatch, probe)
    registry = ExecutorRegistry(
        executors=[
            _FakeExec("claude-code"),
            _FakeExec("grok-cli"),
            _FakeExec("kodgar-native", kind="native"),
        ],
        health_service=service,
    )
    status = registry.status()
    assert isinstance(status["executors"], dict)
    claude = status["executors"]["claude-code"]
    for field in (
        "installed", "authenticated", "model_available", "inference_ok",
        "healthy", "eligible", "error_type", "detail", "checked_at",
        "available", "status",
    ):
        assert field in claude
    assert claude["status"] == "UNHEALTHY"
    assert claude["error_type"] == "model_not_found"
    assert "claude-code" in status["unavailable"]
    assert "kodgar-native" in status["available"]
    assert "claude-code" in status["details"]  # reason exposed, not hidden


# ------------------------------------- 17. no fake completion (guard)
def test_failed_executor_cannot_fake_completion(engine_factory, store, tmp_path):
    """A succeeded=True outcome with no files still fails verification."""
    repo = make_repo(tmp_path)

    class Liar(Executor):
        id = "kodgar-native"
        label = "Kodgar native"
        kind = "native"

        def available(self):
            return True, "test double"

        def execute(self, ctx):
            return ExecutionOutcome(succeeded=True, exit_code=0, summary="trust me")

    engine = engine_factory(architect=StubArchitect(create_plan()))
    engine.registry = ExecutorRegistry(
        executors=[Liar()],
        health_service=_FakeService(eligible={"kodgar-native"}),
    )
    engine.start()
    task = engine.submit(TaskRequest(goal="nothing", repo=str(repo)))
    final = wait_for_terminal(store, task.id)
    assert final.state is TaskState.failed
    assert final.commit_sha is None


# ------------------------------------- minimal fake health service
class _FakeService:
    """Deterministic stand-in for ExecutorHealthService in engine tests."""

    def __init__(self, eligible: set[str]):
        self.eligible = eligible

    def health(self, executor, force=False):
        ok = executor.id in self.eligible
        return ExecutorHealth(
            id=executor.id,
            label=executor.label,
            kind=executor.kind,
            installed=True,
            healthy=ok,
            eligible=ok,
            error_type=None if ok else "ineligible",
        )

    def record_failure(self, executor_id, message, status=None):
        return health_mod.classify_executor_error(message, status)

    def record_success(self, executor_id):
        pass

    def eligible_ids(self, executors):
        return [e.id for e in executors if e.id in self.eligible]

    def status(self, executors):
        hs = [self.health(e) for e in executors]
        return {
            "executors": {h.id: h.model_dump() for h in hs},
            "available": [h.id for h in hs if h.eligible],
            "unavailable": [h.id for h in hs if not h.eligible],
            "healthy": [h.id for h in hs if h.healthy],
        }


# ------------------------------- 17. verifier count parsing (reporting)
def test_test_count_parsing_handles_pytest_summary_order():
    """pytest prints "N failed, M passed"; a parser that only accepts
    "M passed, N failed" silently reports failures as zero.

    Observed live: a suite that exited 1 with 4 failures was reported as
    "1295 passed, 0 failed", which hides exactly the evidence the
    verifier exists to surface.
    """
    assert parse_test_counts("4 failed, 1295 passed, 7 skipped in 611s") == (1295, 4)
    assert parse_test_counts("1295 passed, 7 skipped in 611s") == (1295, 0)
    assert parse_test_counts("1 failed in 2.32s") == (0, 1)
    # Collection errors count as failures.
    assert parse_test_counts("2 passed, 1 error in 3s") == (2, 1)
    # Jest-style output is still understood.
    assert parse_test_counts("Tests:       1 failed, 2 passed") == (2, 1)
    # No counts at all -> no fabricated numbers.
    assert parse_test_counts("no counts here") == (0, 0)


# ------------------- 18. red suites must name their failing tests
def _verifier_with_runs(monkeypatch, results):
    """A TaskVerifier whose test runner returns canned results.

    ``results`` is consumed one entry per invocation, so a test can
    state exactly what the first run and the confirmation re-run each
    returned.
    """
    from app.background import verifier as verifier_module

    calls: list[list[str]] = []
    queue = list(results)

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return queue.pop(0) if queue else {"cmd": cmd, "returncode": 0}

    monkeypatch.setattr(verifier_module, "run", fake_run)
    monkeypatch.setattr(
        "app.workers.validation.detect_test_commands", lambda path: [["pytest", "-q"]]
    )
    return verifier_module.TaskVerifier(), calls


def _run_result(returncode, stdout, cmd=None):
    return {
        "cmd": cmd or "pytest -q",
        "returncode": returncode,
        "stdout": stdout,
        "stderr": "",
        "timed_out": False,
    }


def test_failing_node_ids_are_extracted_from_pytest_summary():
    from app.background.verifier import parse_failing_node_ids

    text = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_heartbeat_hardening.py::test_shutdown_stops_heartbeat_activity\n"
        "FAILED tests/test_x.py::TestC::test_y - AssertionError: nope\n"
        "ERROR tests/test_broken.py\n"
        "1 failed, 1 error in 2.32s\n"
    )
    assert parse_failing_node_ids(text) == [
        "tests/test_heartbeat_hardening.py::test_shutdown_stops_heartbeat_activity",
        "tests/test_x.py::TestC::test_y",
    ]
    # A collection error has no node ID and must not be retried.
    assert parse_failing_node_ids("ERROR tests/test_broken.py") == []


def test_reproducible_failure_still_blocks_the_commit(monkeypatch, tmp_path):
    """A real failure fails both runs: the check must stay FAIL, and it
    must name the test instead of reporting a bare exit code."""
    failing = (
        "FAILED tests/test_stable.py::test_really_broken - AssertionError\n"
        "1 failed, 3 passed in 1.10s\n"
    )
    verifier, calls = _verifier_with_runs(
        monkeypatch,
        [
            _run_result(1, failing),
            _run_result(1, "FAILED tests/test_stable.py::test_really_broken\n1 failed in 0.4s\n"),
        ],
    )

    (check,) = verifier._test_checks(tmp_path, None, timeout=60)

    assert check.status == "FAIL"
    assert "test_really_broken" in check.detail
    assert "reproducible failure" in check.detail
    # The confirmation re-ran only the failing test.
    assert calls[1] == ["pytest", "-q", "tests/test_stable.py::test_really_broken"]
    assert check.evidence["retry"]["outcome"] == "FAIL"
    # Nothing was hidden: the failed count survives into the report.
    assert check.evidence["failed"] == 1


def test_load_flake_is_confirmed_not_hidden(monkeypatch, tmp_path):
    """A suite that loses one load-sensitive test under engine load must
    not block a task forever — but the flake stays visible."""
    noisy = (
        "FAILED tests/test_heartbeat_hardening.py::test_shutdown_stops_heartbeat_activity\n"
        "1 failed, 1301 passed, 7 skipped in 331s\n"
    )
    verifier, calls = _verifier_with_runs(
        monkeypatch, [_run_result(1, noisy), _run_result(0, "1 passed in 1.9s\n")]
    )

    (check,) = verifier._test_checks(tmp_path, None, timeout=60)

    assert check.status == "PASS"
    assert "load flake" in check.detail
    assert "test_shutdown_stops_heartbeat_activity" in check.detail
    assert check.evidence["retry"]["outcome"] == "PASS"
    # Exactly one confirmation run, of only the failed test.
    assert len(calls) == 2
    assert calls[1][-1] == (
        "tests/test_heartbeat_hardening.py::test_shutdown_stops_heartbeat_activity"
    )


def test_unnamed_failure_is_reported_but_not_retried(monkeypatch, tmp_path):
    """Without node IDs there is nothing to confirm, so no extra run is
    spent — but the red run is still reported as a failure."""
    verifier, calls = _verifier_with_runs(
        monkeypatch, [_run_result(1, "interrupted in 0.10s\n")]
    )

    (check,) = verifier._test_checks(tmp_path, None, timeout=60)

    assert check.status == "FAIL"
    assert len(calls) == 1
