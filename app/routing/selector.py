"""Deterministic adaptive selection with optional history weighting."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.routing.capabilities import ModelCapability, ModelCapabilityRegistry
from app.routing.history import HistoryStore
from app.routing.policy import BudgetPolicy
from app.routing.profiling import TaskProfile

W_CODING = 3.0
W_REASONING = 3.0
W_TOOLS = 1.5
W_CONTEXT = 2.0
W_RELIABILITY = 2.0
W_LATENCY = 1.0
W_COST = 1.0

_NO_ELIGIBLE = "no eligible model for task profile and budget policy"


@dataclass
class ScoredCandidate:
    capability: ModelCapability
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class RoutingDecision:
    provider: str
    model: str
    score: float
    reason: str
    fallback_chain: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def selected_key(self) -> str:
        return f"{self.provider}/{self.model}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "score": self.score,
            "reason": self.reason,
            "fallback_chain": list(self.fallback_chain),
            "scores": dict(self.scores),
        }


class RoutingError(RuntimeError):
    pass


class AdaptiveRouter:
    """Score every eligible model and return a bounded fallback chain."""

    def __init__(
        self,
        registry: ModelCapabilityRegistry | None = None,
        history: HistoryStore | None = None,
        max_fallbacks: int = 3,
    ):
        from app.routing.capabilities import default_registry

        self.registry = registry or default_registry()
        self.history = history
        self.max_fallbacks = max(1, int(max_fallbacks))

    def eligible(
        self, profile: TaskProfile, policy: BudgetPolicy | None = None
    ) -> list[ModelCapability]:
        policy = policy or BudgetPolicy()
        out: list[ModelCapability] = []
        for capability in self.registry.all():
            if not policy.allows(
                capability.provider, capability.model, capability.local
            ):
                continue
            if capability.context_window < profile.estimated_context:
                continue
            if capability.coding_score < policy.min_coding:
                continue
            if capability.reasoning_score < policy.min_reasoning:
                continue
            if policy.requires_tools and not capability.tool_support:
                continue
            if (
                profile.tool_requirement >= 0.8
                and not capability.tool_support
            ):
                continue
            if (
                policy.max_cost is not None
                and capability.cost_per_task > policy.max_cost
            ):
                continue
            if (
                policy.max_latency_ms is not None
                and capability.effective_latency_ms() > policy.max_latency_ms
            ):
                continue
            out.append(capability)
        return out

    def score(
        self,
        capability: ModelCapability,
        profile: TaskProfile,
        policy: BudgetPolicy | None = None,
    ) -> ScoredCandidate:
        policy = policy or BudgetPolicy()
        reasons: list[str] = []
        score = 0.0
        score += W_CODING * capability.coding_score * max(
            0.2, profile.coding_requirement
        )
        reasons.append(f"coding {capability.coding_score:.2f}")
        score += W_REASONING * capability.reasoning_score * max(
            0.2, profile.reasoning_requirement
        )
        reasons.append(f"reasoning {capability.reasoning_score:.2f}")
        if capability.tool_support:
            score += W_TOOLS * max(0.2, profile.tool_requirement)
            reasons.append("tool support")
        if capability.context_window >= profile.estimated_context:
            headroom = capability.context_window - profile.estimated_context
            score += W_CONTEXT * min(1.0, headroom / 100000.0 + 0.5)
            reasons.append(f"context {capability.context_window}")
        score += W_RELIABILITY * capability.observed_reliability()
        reasons.append(f"reliability {capability.observed_reliability():.2f}")
        latency = capability.effective_latency_ms()
        latency_bonus = max(0.0, 1.0 - latency / 10000.0)
        latency_weight = 0.3 + profile.latency_sensitivity
        score += W_LATENCY * latency_bonus * latency_weight
        cost_weight = 0.3 + profile.cost_sensitivity
        cost_bonus = 1.0 / (1.0 + capability.cost_per_task * 20.0)
        score += W_COST * cost_bonus * cost_weight
        reasons.append(f"latency {latency:.0f}ms cost {capability.cost_per_task:.4f}")
        history_bonus = 0.0
        if self.history is not None:
            stats = self.history.stats_for(
                capability.provider, capability.model
            )
            if stats is not None and stats.samples > 0:
                history_bonus = (stats.success_rate - 0.5) * 2.0
                score += history_bonus
                reasons.append(
                    f"history {stats.success_rate:.2f}/{stats.samples}"
                )
            recent_failures = (
                self.history.recent_failures(
                    capability.provider, capability.model
                )
                if hasattr(self.history, "recent_failures")
                else 0
            )
            if recent_failures:
                penalty = min(2.0, 0.5 * recent_failures + 0.1 * profile.retry_count)
                score -= penalty
                reasons.append(f"recent failures {recent_failures}")
        if policy.prefer_local or policy.local_only:
            if capability.local and capability.meets_minimum(
                min_context=profile.estimated_context,
                min_coding=max(policy.min_coding, 0.3),
                min_reasoning=max(policy.min_reasoning, 0.3),
                needs_tools=policy.requires_tools
                or profile.tool_requirement >= 0.8,
            ):
                score += 1.5
                reasons.append("local-first bonus")
        return ScoredCandidate(
            capability=capability, score=score, reasons=reasons
        )

    def select(
        self,
        profile: TaskProfile,
        policy: BudgetPolicy | None = None,
    ) -> RoutingDecision:
        policy = policy or BudgetPolicy()
        candidates = self.eligible(profile, policy)
        if not candidates:
            raise RoutingError(_NO_ELIGIBLE)
        scored = [self.score(item, profile, policy) for item in candidates]
        scored.sort(key=lambda item: (-item.score, item.capability.key()))
        if policy.prefer_local:
            capable_locals = [
                item
                for item in scored
                if item.capability.local
                and item.capability.meets_minimum(
                    min_context=profile.estimated_context,
                    min_coding=max(policy.min_coding, 0.3),
                    min_reasoning=max(policy.min_reasoning, 0.3),
                    needs_tools=policy.requires_tools
                    or profile.tool_requirement >= 0.8,
                )
            ]
            if capable_locals:
                best_local = capable_locals[0]
                scored = [best_local] + [
                    item for item in scored if item is not best_local
                ]
        best = scored[0]
        chain = [item.capability.key() for item in scored[1 : self.max_fallbacks]]
        summary = "; ".join(best.reasons)
        return RoutingDecision(
            provider=best.capability.provider,
            model=best.capability.model,
            score=round(best.score, 4),
            reason=(
                f"Selected {best.capability.key()} for "
                f"{profile.category}/{profile.complexity}: {summary}"
            ),
            fallback_chain=chain,
            scores={item.capability.key(): round(item.score, 4) for item in scored},
        )
