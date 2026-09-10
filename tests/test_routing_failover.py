"""Hermetic tests for routing failover and the agent runtime router."""

import pytest

from app.providers import (
    ANTHROPIC_COMPATIBLE,
    GENERIC_HTTP,
    OLLAMA_LOCAL,
    OPENAI_COMPATIBLE,
    ProviderDescriptor,
)
from app.routing.capabilities import ModelCapability, ModelCapabilityRegistry
from app.routing.failover import (
    FailureKind,
    RoutingExhausted,
    classify_failure,
    execute_with_failover,
)
from app.routing.history import HistoryStore
from app.routing.policy import BudgetPolicy
from app.routing.selector import RoutingError
from app.runtime.router import AgentRouteRequest, AgentRuntimeRouter


class ScriptedError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TimeoutError_(Exception):
    pass


def timeout_error(message: str = "timed out") -> Exception:
    exc = TimeoutError_(message)
    return exc


def make_router() -> AgentRuntimeRouter:
    registry = ModelCapabilityRegistry()
    registry.register(
        ModelCapability(
            provider="primary",
            model="one",
            context_window=128000,
            coding_score=0.9,
            reasoning_score=0.9,
            tool_support=True,
            cost_per_task=0.05,
            latency_p50_ms=1000.0,
            reliability=0.98,
        )
    )
    registry.register(
        ModelCapability(
            provider="backup",
            model="two",
            context_window=128000,
            coding_score=0.8,
            reasoning_score=0.8,
            tool_support=True,
            cost_per_task=0.02,
            latency_p50_ms=1100.0,
            reliability=0.9,
        )
    )
    return AgentRuntimeRouter(registry=registry, history=HistoryStore())


def test_provider_descriptors_cover_all_kinds():
    descriptors = [
        ProviderDescriptor(
            name="oai",
            kind=OPENAI_COMPATIBLE,
            base_url="http://oai",
            default_model="m",
            auth_configured=True,
        ),
        ProviderDescriptor(
            name="ant",
            kind=ANTHROPIC_COMPATIBLE,
            base_url="http://ant",
            default_model="m",
            auth_configured=True,
        ),
        ProviderDescriptor(
            name="ollama",
            kind=OLLAMA_LOCAL,
            base_url="http://127.0.0.1:11434",
            default_model="m",
            local=True,
        ),
        ProviderDescriptor(
            name="generic",
            kind=GENERIC_HTTP,
            base_url="http://generic",
            default_model="m",
            auth_configured=True,
        ),
    ]
    assert {item.kind for item in descriptors} == {
        OPENAI_COMPATIBLE,
        ANTHROPIC_COMPATIBLE,
        OLLAMA_LOCAL,
        GENERIC_HTTP,
    }
    assert all(item.is_available() for item in descriptors)
    missing_auth = ProviderDescriptor(
        name="remote",
        kind=GENERIC_HTTP,
        base_url="http://remote",
        default_model="m",
    )
    assert missing_auth.is_available() is False
    capability = descriptors[0].to_capability()
    assert capability.provider == "oai"
    assert capability.model == "m"


def test_failover_moves_through_chain_on_429_then_500():
    calls: list[str] = []

    def call(key: str) -> str:
        calls.append(key)
        if key == "a/1":
            raise ScriptedError("rate limited", status_code=429)
        if key == "b/2":
            raise ScriptedError("boom", status_code=500)
        return "ok-from-c"

    result = execute_with_failover(["a/1", "b/2", "c/3"], call)
    assert result.selected == "c/3"
    assert result.output == "ok-from-c"
    assert [item["selected"] for item in result.attempts] == [
        "a/1",
        "b/2",
        "c/3",
    ]
    assert calls == ["a/1", "b/2", "c/3"]


def test_failover_on_timeout_and_unavailable():
    attempts = {"n": 0}

    def call(key: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise timeout_error("provider timed out")
        if attempts["n"] == 2:
            raise ScriptedError("connection refused")
        return "recovered"

    result = execute_with_failover(["a/1", "b/2", "c/3"], call)
    assert result.output == "recovered"
    assert len(result.attempts) == 3


def test_deterministic_failure_does_not_fail_over():
    def call(key: str) -> str:
        raise ScriptedError("bad request", status_code=400)

    with pytest.raises(ScriptedError):
        execute_with_failover(["a/1", "b/2"], call)


def test_exhaustion_becomes_blocked_external():
    def call(key: str) -> str:
        raise ScriptedError("down", status_code=503)

    with pytest.raises(RoutingExhausted) as exc_info:
        execute_with_failover(["a/1", "b/2"], call)
    assert len(exc_info.value.attempts) == 2


def test_classify_failure_kinds():
    assert classify_failure(timeout_error()) == FailureKind.TIMEOUT
    assert (
        classify_failure(ScriptedError("limited", status_code=429))
        == FailureKind.RATE_LIMITED
    )
    assert (
        classify_failure(ScriptedError("bad", status_code=500))
        == FailureKind.SERVER_ERROR
    )
    assert (
        classify_failure(ScriptedError("no key", status_code=401))
        == FailureKind.AUTH_UNAVAILABLE
    )
    assert (
        classify_failure(ScriptedError("connection refused"))
        == FailureKind.PROVIDER_UNAVAILABLE
    )
    assert (
        classify_failure(ScriptedError("bad request", status_code=400))
        == FailureKind.DETERMINISTIC
    )


def test_runtime_run_records_history_and_falls_back():
    router = make_router()

    def call(key: str) -> str:
        if key == "primary/one":
            raise ScriptedError("busy", status_code=429)
        return "backup-output"

    result = router.run(AgentRouteRequest(task="Add a helper"), call)
    assert result.blocked_external is False
    assert result.execution is not None
    assert result.execution.output == "backup-output"
    assert result.decision.selected_key() == "primary/one"
    stats = router.history.stats_for("backup", "two")
    assert stats is not None and stats.successes == 1


def test_runtime_run_marks_blocked_external_after_all_fail():
    router = make_router()

    def call(key: str) -> str:
        raise ScriptedError("down", status_code=503)

    result = router.run(AgentRouteRequest(task="Add a helper"), call)
    assert result.blocked_external is True
    assert result.execution is not None
    assert result.execution.output is None
    assert len(result.execution.attempts) == 2


def test_runtime_route_reports_no_eligible_model():
    router = make_router()
    request = AgentRouteRequest(
        task="Add a helper",
        policy=BudgetPolicy(allowed_providers=("ghost",)),
    )
    with pytest.raises(RoutingError):
        router.route(request)
