"""Budget and placement policy for routing."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BudgetPolicy:
    """Hard constraints applied before scoring."""

    max_cost: float | None = None
    max_latency_ms: float | None = None
    allowed_providers: tuple[str, ...] | None = None
    denied_providers: tuple[str, ...] = ()
    local_only: bool = False
    remote_only: bool = False
    prefer_local: bool = False
    min_coding: float = 0.0
    min_reasoning: float = 0.0
    requires_tools: bool = False
    excluded_models: tuple[str, ...] = field(default_factory=tuple)

    def allows(self, provider: str, model: str, local: bool) -> bool:
        if self.allowed_providers is not None and provider not in self.allowed_providers:
            return False
        if provider in self.denied_providers:
            return False
        if f"{provider}/{model}" in self.excluded_models:
            return False
        if self.local_only and not local:
            return False
        if self.remote_only and local:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "max_cost": self.max_cost,
            "max_latency_ms": self.max_latency_ms,
            "allowed_providers": list(self.allowed_providers or [])
            or None,
            "denied_providers": list(self.denied_providers),
            "local_only": self.local_only,
            "remote_only": self.remote_only,
            "prefer_local": self.prefer_local,
            "min_coding": self.min_coding,
            "min_reasoning": self.min_reasoning,
            "requires_tools": self.requires_tools,
            "excluded_models": list(self.excluded_models),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BudgetPolicy":
        allowed = data.get("allowed_providers")
        return cls(
            max_cost=data.get("max_cost"),
            max_latency_ms=data.get("max_latency_ms"),
            allowed_providers=tuple(allowed) if allowed else None,
            denied_providers=tuple(data.get("denied_providers") or ()),
            local_only=bool(data.get("local_only", False)),
            remote_only=bool(data.get("remote_only", False)),
            prefer_local=bool(data.get("prefer_local", False)),
            min_coding=float(data.get("min_coding", 0.0)),
            min_reasoning=float(data.get("min_reasoning", 0.0)),
            requires_tools=bool(data.get("requires_tools", False)),
            excluded_models=tuple(data.get("excluded_models") or ()),
        )
