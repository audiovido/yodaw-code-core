"""Provider descriptors for model routing.

A descriptor names one reachable backend and the protocol it
speaks. The core selector never branches on vendor names; it only
reads these generic fields.
"""

from __future__ import annotations

from dataclasses import dataclass

OPENAI_COMPATIBLE = "openai_compatible"
ANTHROPIC_COMPATIBLE = "anthropic_compatible"
OLLAMA_LOCAL = "ollama_local"
GENERIC_HTTP = "generic_http"

KINDS = (
    OPENAI_COMPATIBLE,
    ANTHROPIC_COMPATIBLE,
    OLLAMA_LOCAL,
    GENERIC_HTTP,
)


@dataclass
class ProviderDescriptor:
    """One reachable execution backend."""

    name: str
    kind: str
    base_url: str
    default_model: str
    local: bool = False
    available: bool = True
    auth_configured: bool = False
    cost_per_task: float = 0.0
    latency_p50_ms: float = 500.0
    context_window: int = 8192
    coding_score: float = 0.5
    reasoning_score: float = 0.5
    tool_support: bool = False
    reliability: float = 0.9

    def is_available(self) -> bool:
        """Reachable and credentialed (local needs no credentials)."""
        if not self.available:
            return False
        if self.local:
            return True
        return self.auth_configured

    def to_capability(self, model: Optional[str] = None):
        """Project this descriptor into registry metadata."""
        from app.routing.capabilities import ModelCapability

        return ModelCapability(
            provider=self.name,
            model=model or self.default_model,
            context_window=self.context_window,
            coding_score=self.coding_score,
            reasoning_score=self.reasoning_score,
            tool_support=self.tool_support,
            local=self.local,
            cost_per_task=self.cost_per_task,
            latency_p50_ms=self.latency_p50_ms,
            reliability=self.reliability,
        )
