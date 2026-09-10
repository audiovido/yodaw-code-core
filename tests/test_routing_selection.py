"""Hermetic tests for adaptive routing: selection and policy."""

import pytest

from app.routing.capabilities import ModelCapability, ModelCapabilityRegistry
from app.routing.history import HistoryStore
from app.routing.policy import BudgetPolicy
from app.routing.profiling import build_task_profile
from app.routing.selector import AdaptiveRouter, RoutingError


def make_registry() -> ModelCapabilityRegistry:
    registry = ModelCapabilityRegistry()
    registry.register(
        ModelCapability(
            provider="local-ollama",
            model="tiny",
            context_window=8192,
            coding_score=0.5,
            reasoning_score=0.4,
            tool_support=False,
            local=True,
            cost_per_task=0.0,
            latency_p50_ms=800.0,
            reliability=0.85,
        )
    )
    registry.register(
        ModelCapability(
            provider="remote-openai",
            model="strong",
            context_window=128000,
            coding_score=0.9,
            reasoning_score=0.9,
            tool_support=True,
            local=False,
            cost_per_task=0.05,
            latency_p50_ms=1200.0,
            reliability=0.98,
        )
    )
    registry.register(
        ModelCapability(
            provider="remote-anthropic",
            model="reasoner",
            context_window=200000,
            coding_score=0.88,
            reasoning_score=0.95,
            tool_support=True,
            local=False,
            cost_per_task=0.06,
            latency_p50_ms=1500.0,
            reliability=0.97,
        )
    )
    return registry


def test_task_profile_structure():
    profile = build_task_profile(
        "Fix the divide by zero crash in calculator.py",
        files_changed=1,
    )
    assert profile.category == "bugfix"
    assert profile.complexity in (
        "trivial",
        "simple",
        "medium",
        "complex",
        "hard",
    )
    assert profile.estimated_context >= 1000
    assert 0.0 <= profile.reasoning_requirement <= 1.0
    assert 0.0 <= profile.coding_requirement <= 1.0
    assert 0.0 <= profile.tool_requirement <= 1.0
    assert 0.0 <= profile.latency_sensitivity <= 1.0
    assert 0.0 <= profile.cost_sensitivity <= 1.0
    assert profile.language == "python"


def test_selection_returns_decision_with_fallback_chain():
    router = AdaptiveRouter(registry=make_registry())
    profile = build_task_profile("Add a power function with tests")
    decision = router.select(profile)
    assert decision.provider
    assert decision.model
    assert decision.score > 0
    assert decision.reason
    assert decision.fallback_chain
    assert decision.selected_key() not in decision.fallback_chain
    assert decision.selected_key() in decision.scores


def test_cost_constraint_excludes_expensive_models():
    router = AdaptiveRouter(registry=make_registry())
    profile = build_task_profile("Add a tiny helper")
    decision = router.select(profile, BudgetPolicy(max_cost=0.0))
    assert decision.provider == "local-ollama"
    assert decision.model == "tiny"


def test_latency_constraint_excludes_slow_models():
    registry = ModelCapabilityRegistry()
    registry.register(
        ModelCapability(
            provider="fast",
            model="quick",
            latency_p50_ms=200.0,
            coding_score=0.7,
            reasoning_score=0.7,
        )
    )
    registry.register(
        ModelCapability(
            provider="slow",
            model="sloth",
            latency_p50_ms=9000.0,
            coding_score=0.9,
            reasoning_score=0.9,
        )
    )
    router = AdaptiveRouter(registry=registry)
    profile = build_task_profile("Fix a typo")
    decision = router.select(profile, BudgetPolicy(max_latency_ms=1000.0))
    assert decision.provider == "fast"


def test_unavailable_provider_is_excluded():
    registry = make_registry()
    router = AdaptiveRouter(registry=registry)
    profile = build_task_profile("Add a power function with tests")
    policy = BudgetPolicy(denied_providers=("remote-openai", "remote-anthropic"))
    decision = router.select(profile, policy)
    assert decision.provider == "local-ollama"


def test_local_first_prefers_capable_local():
    router = AdaptiveRouter(registry=make_registry())
    profile = build_task_profile("Rename a helper")
    decision = router.select(profile, BudgetPolicy(prefer_local=True))
    assert decision.provider == "local-ollama"


def test_local_first_falls_back_when_local_lacks_tools():
    router = AdaptiveRouter(registry=make_registry())
    profile = build_task_profile(
        "Complex multi-file migration needing tools",
        files_changed=6,
        tools_required=True,
    )
    decision = router.select(profile, BudgetPolicy(prefer_local=True))
    assert decision.provider.startswith("remote-")


def test_capability_mismatch_excludes_small_context():
    registry = ModelCapabilityRegistry()
    registry.register(
        ModelCapability(
            provider="small",
            model="tiny",
            context_window=1000,
            coding_score=0.9,
            reasoning_score=0.9,
        )
    )
    registry.register(
        ModelCapability(
            provider="big",
            model="large",
            context_window=200000,
            coding_score=0.8,
            reasoning_score=0.8,
        )
    )
    router = AdaptiveRouter(registry=registry)
    profile = build_task_profile("x" * 5000, files_changed=8)
    assert profile.estimated_context > 1000
    decision = router.select(profile)
    assert decision.provider == "big"


def test_deterministic_tie_breaking_by_key():
    registry = ModelCapabilityRegistry()
    for name in ("b-provider", "a-provider"):
        registry.register(
            ModelCapability(
                provider=name,
                model="same",
                coding_score=0.7,
                reasoning_score=0.7,
            )
        )
    router = AdaptiveRouter(registry=registry)
    profile = build_task_profile("Add a helper")
    first = router.select(profile)
    second = router.select(profile)
    assert first.selected_key() == second.selected_key() == "a-provider/same"


def test_no_eligible_model_raises():
    router = AdaptiveRouter(registry=make_registry())
    profile = build_task_profile("Add a helper")
    with pytest.raises(RoutingError):
        router.select(profile, BudgetPolicy(allowed_providers=("missing",)))


def test_corrupted_history_is_skipped(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(
        '{"records": ['
        '{"provider": "remote-openai", "model": "strong", '
        '"success": true, "latency_ms": 100.0}, '
        '{"broken": true}, '
        '{"provider": "x"}]}'
    )
    store = HistoryStore(path)
    assert len(store.records) == 1
    assert store.skipped == 2
    stats = store.stats_for("remote-openai", "strong")
    assert stats is not None and stats.samples == 1


def test_history_weighting_prefers_proven_model():
    registry = ModelCapabilityRegistry()
    for name in ("alpha", "beta"):
        registry.register(
            ModelCapability(
                provider=name,
                model="m",
                coding_score=0.7,
                reasoning_score=0.7,
            )
        )
    history = HistoryStore()
    for _ in range(4):
        history.record_result("beta", "m", success=True, latency_ms=100.0)
    history.record_result("alpha", "m", success=False, latency_ms=100.0)
    router = AdaptiveRouter(registry=registry, history=history)
    profile = build_task_profile("Add a helper")
    decision = router.select(profile)
    assert decision.provider == "beta"
