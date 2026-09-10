"""Agent runtime router: profile, select, execute with failover."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.routing.capabilities import ModelCapabilityRegistry, default_registry
from app.routing.failover import (
    FailoverResult,
    RoutingExhausted,
    execute_with_failover,
)
from app.routing.history import HistoryStore
from app.routing.policy import BudgetPolicy
from app.routing.profiling import TaskProfile, build_task_profile
from app.routing.selector import AdaptiveRouter, RoutingDecision


@dataclass
class AgentRouteRequest:
    task: str
    category_hint: str | None = None
    complexity_hint: str | None = None
    language_hint: str | None = None
    framework: str | None = None
    files_changed: int = 1
    history_tokens: int = 0
    latency_sensitivity: float = 0.5
    cost_sensitivity: float = 0.5
    tools_required: bool = False
    retry_count: int = 0
    policy: BudgetPolicy = field(default_factory=BudgetPolicy)


@dataclass
class AgentRouteResult:
    profile: TaskProfile
    decision: RoutingDecision
    execution: FailoverResult | None = None
    blocked_external: bool = False


class AgentRuntimeRouter:
    """One entrypoint agents use to pick a backend and run it."""

    def __init__(
        self,
        registry: ModelCapabilityRegistry | None = None,
        history: HistoryStore | None = None,
        max_fallbacks: int = 3,
    ):
        self.registry = registry or default_registry()
        self.history = history or HistoryStore()
        self.router = AdaptiveRouter(
            registry=self.registry,
            history=self.history,
            max_fallbacks=max_fallbacks,
        )

    def route(self, request: AgentRouteRequest) -> AgentRouteResult:
        profile = build_task_profile(
            request.task,
            category_hint=request.category_hint,
            complexity_hint=request.complexity_hint,
            language_hint=request.language_hint,
            framework=request.framework,
            files_changed=request.files_changed,
            history_tokens=request.history_tokens,
            latency_sensitivity=request.latency_sensitivity,
            cost_sensitivity=request.cost_sensitivity,
            tools_required=request.tools_required,
            retry_count=request.retry_count,
        )
        decision = self.router.select(profile, request.policy)
        return AgentRouteResult(profile=profile, decision=decision)

    def run(
        self, request: AgentRouteRequest, call, record_history: bool = True
    ) -> AgentRouteResult:
        """Route once, then execute across the bounded fallback chain."""
        routed = self.route(request)
        chain = [routed.decision.selected_key()] + list(
            routed.decision.fallback_chain
        )
        try:
            execution = execute_with_failover(chain, call)
        except RoutingExhausted as exc:
            if record_history:
                for attempt in exc.attempts:
                    selected = attempt.get("selected", "")
                    if "/" in selected:
                        provider, model = selected.split("/", 1)
                        self.history.record_result(
                            provider, model, success=False
                        )
            routed.blocked_external = True
            routed.execution = FailoverResult(
                selected="", attempts=exc.attempts, output=None
            )
            return routed
        if record_history:
            for attempt in execution.attempts:
                selected = attempt.get("selected", "")
                if "/" in selected:
                    provider, model = selected.split("/", 1)
                    self.history.record_result(
                        provider, model, success=attempt.get("ok", False)
                    )
        routed.execution = execution
        return routed
