"""Model capability registry for adaptive routing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ModelCapability:
    """Capability metadata for one provider and model pair."""

    provider: str
    model: str
    context_window: int = 8192
    coding_score: float = 0.5
    reasoning_score: float = 0.5
    tool_support: bool = False
    local: bool = False
    cost_per_task: float = 0.0
    latency_p50_ms: float = 500.0
    reliability: float = 0.9
    failure_count: int = 0
    success_count: int = 0
    avg_latency_ms: float | None = None

    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def meets_minimum(
        self,
        min_context: int = 0,
        min_coding: float = 0.0,
        min_reasoning: float = 0.0,
        needs_tools: bool = False,
    ) -> bool:
        if self.context_window < min_context:
            return False
        if self.coding_score < min_coding:
            return False
        if self.reasoning_score < min_reasoning:
            return False
        if needs_tools and not self.tool_support:
            return False
        return True

    def observed_reliability(self) -> float:
        total = self.success_count + self.failure_count
        if total <= 0:
            return self.reliability
        return self.success_count / total

    def effective_latency_ms(self) -> float:
        if self.avg_latency_ms is not None:
            return self.avg_latency_ms
        return self.latency_p50_ms

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "context_window": self.context_window,
            "coding_score": self.coding_score,
            "reasoning_score": self.reasoning_score,
            "tool_support": self.tool_support,
            "local": self.local,
            "cost_per_task": self.cost_per_task,
            "latency_p50_ms": self.latency_p50_ms,
            "reliability": self.reliability,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "avg_latency_ms": self.avg_latency_ms,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelCapability":
        known = {
            "provider",
            "model",
            "context_window",
            "coding_score",
            "reasoning_score",
            "tool_support",
            "local",
            "cost_per_task",
            "latency_p50_ms",
            "reliability",
            "failure_count",
            "success_count",
            "avg_latency_ms",
        }
        clean = {key: value for key, value in data.items() if key in known}
        return cls(**clean)


@dataclass
class ModelCapabilityRegistry:
    """Deterministic store of model capabilities."""

    models: dict[str, ModelCapability] = field(default_factory=dict)

    def register(self, capability: ModelCapability) -> None:
        self.models[capability.key()] = capability

    def get(self, provider: str, model: str) -> ModelCapability | None:
        return self.models.get(f"{provider}/{model}")

    def all(self) -> list[ModelCapability]:
        return sorted(
            self.models.values(), key=lambda item: item.key()
        )

    def mark_available(self, provider: str, available: bool) -> None:
        for capability in self.models.values():
            if capability.provider == provider:
                capability.reliability = (
                    capability.reliability if available else 0.0
                )

    def record_outcome(
        self, provider: str, model: str, success: bool
    ) -> None:
        capability = self.get(provider, model)
        if capability is None:
            return
        if success:
            capability.success_count += 1
        else:
            capability.failure_count += 1

    def to_dict(self) -> dict:
        return {
            key: capability.to_dict()
            for key, capability in sorted(self.models.items())
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ModelCapabilityRegistry":
        registry = cls()
        models = data.get("models", data) if isinstance(data, dict) else {}
        for key, payload in models.items():
            try:
                capability = ModelCapability.from_dict(payload)
            except TypeError:
                continue
            registry.models[capability.key()] = capability
        return registry


def default_registry() -> ModelCapabilityRegistry:
    """Seeded registry with generic local and remote entries."""
    registry = ModelCapabilityRegistry()
    registry.register(
        ModelCapability(
            provider="local-ollama",
            model="local-code",
            context_window=8192,
            coding_score=0.55,
            reasoning_score=0.45,
            tool_support=False,
            local=True,
            cost_per_task=0.0,
            latency_p50_ms=900.0,
            reliability=0.85,
        )
    )
    registry.register(
        ModelCapability(
            provider="remote-openai",
            model="remote-code",
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
            model="remote-reason",
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
