"""Executor registry: health-gated selection and ordered fallback chains.

The registry is the only place that decides which coding agent may
receive a task. Two hard rules shape it:

- **Health is evidence, not binary detection.** Selection consults the
  ``ExecutorHealthService`` (real inference probes + circuit breaker);
  an executor whose binary exists but whose model/quota/provider is
  broken is *ineligible*, exactly like a missing binary.
- **Absence or sickness degrades, it does not fail.** The planner's
  choice is honored only when healthy; otherwise the first eligible
  fallback takes over and the reason records the health rejection.

``select_chain`` returns the full ordered fallback pool so the engine
can attempt up to N distinct executors for one task without ever
re-entering a known-broken one.
"""

from __future__ import annotations

import os
from typing import Optional

from app.background.executors.base import Executor
from app.background.executors.claude_code import ClaudeCodeExecutor
from app.background.executors.codex import CodexExecutor
from app.background.executors.grok_cli import GrokCliExecutor
from app.background.executors.health import ExecutorHealthService
from app.background.executors.kodgar_native import KodgarNativeExecutor
from app.background.models import PlanResult

# Ordered fallbacks: when the planner's executor is unavailable or
# unhealthy, the first *eligible* entry here takes over. Native is last
# because it depends only on the project's own engine, so it is always
# a valid backstop.
FALLBACK_ORDER = ("claude-code", "codex", "grok-cli", "kodgar-native")

# Task-type heuristics used only when the planner is silent or its
# choice is impossible. These are defaults for V1, not permanent
# truths: the planner's own reasoning wins whenever it is valid.
DEFAULT_BY_TASK_TYPE = {
    "fullstack_feature": "claude-code",
    "feature": "claude-code",
    "bugfix": "codex",
    "debugging": "codex",
    "research": "grok-cli",
    "architecture": "grok-cli",
    "simple": "kodgar-native",
    "local_private": "kodgar-native",
    "file_creation": "kodgar-native",
}

_health_service: Optional[ExecutorHealthService] = None


def get_health_service() -> ExecutorHealthService:
    """Process-wide health service (env-tunable, lazily built)."""
    global _health_service
    if _health_service is None:
        _health_service = ExecutorHealthService(
            ttl_seconds=float(os.environ.get("KODGAR_HEALTH_TTL", "120") or 120),
            cooldown_seconds=float(
                os.environ.get("KODGAR_HEALTH_COOLDOWN", "300") or 300
            ),
            failure_threshold=int(
                os.environ.get("KODGAR_HEALTH_FAILURE_THRESHOLD", "2") or 2
            ),
            probe_timeout=float(
                os.environ.get("KODGAR_HEALTH_PROBE_TIMEOUT", "45") or 45
            ),
        )
    return _health_service


class ExecutorRegistry:
    """All known executors plus the health-gated routing decision."""

    def __init__(
        self,
        executors: Optional[list[Executor]] = None,
        health_service: Optional[ExecutorHealthService] = None,
    ):
        self.executors: list[Executor] = executors or [
            ClaudeCodeExecutor(),
            CodexExecutor(),
            GrokCliExecutor(),
            KodgarNativeExecutor(),
        ]
        self._by_id = {executor.id: executor for executor in self.executors}
        self.health_service = health_service or get_health_service()

    # ---------------------------------------------------------- read
    def get(self, executor_id: str) -> Optional[Executor]:
        return self._by_id.get(executor_id)

    def infos(self) -> list:
        """Legacy binary-level descriptors (cheap, no probes)."""
        return [executor.info() for executor in self.executors]

    def availability(self) -> dict:
        return {info.id: info for info in self.infos()}

    def available_ids(self) -> list[str]:
        return [info.id for info in self.infos() if info.available]

    # -------------------------------------------------------- health
    def health_of(self, executor_id: str):
        executor = self._by_id.get(executor_id)
        if executor is None:
            return None
        return self.health_service.health(executor)

    def eligible_ids(self) -> list[str]:
        """Executors that may receive work right now (health-gated)."""
        return self.health_service.eligible_ids(self.executors)

    def status(self) -> dict:
        """Health-shaped status for the API and the UI.

        ``executors`` is a map keyed by id (the Terminal UI iterates
        ``Object.entries``), each entry carrying the full health ladder
        plus the legacy ``available`` flag so old consumers keep
        working. Reasons are never hidden.
        """
        healths = [
            self.health_service.health(executor) for executor in self.executors
        ]
        as_map = {}
        for health in healths:
            entry = health.model_dump()
            entry["available"] = health.installed
            entry["status"] = (
                "HEALTHY" if health.healthy and health.eligible
                else ("UNHEALTHY" if health.error_type else "UNKNOWN")
            )
            as_map[health.id] = entry
        eligible = [h.id for h in healths if h.eligible]
        return {
            "executors": as_map,
            "available": eligible,
            "unavailable": [h.id for h in healths if not h.eligible],
            "healthy": [h.id for h in healths if h.healthy],
            "details": {
                h.id: (h.detail or h.error_type or "")
                for h in healths
                if not h.eligible
            },
        }

    def refresh(self) -> dict:
        """Force a re-probe of every executor (bypasses the TTL)."""
        for executor in self.executors:
            self.health_service.health(executor, force=True)
        return self.status()

    # -------------------------------------------------------- select
    def select(
        self,
        plan: PlanResult,
        preference: Optional[str] = None,
    ) -> tuple[str, str]:
        """Resolve ``(executor_id, reason)`` for a validated plan.

        Health-gated: a planner choice or user override naming an
        unhealthy executor is recorded and rejected, and the first
        eligible fallback is used instead.
        """
        chain, reason = self.select_chain(plan, preference=preference)
        if not chain:
            raise RuntimeError(
                "no healthy executor is eligible on this machine: "
                + self._eligibility_summary()
            )
        return chain[0], reason

    def select_chain(
        self,
        plan: PlanResult,
        preference: Optional[str] = None,
    ) -> tuple[list[str], str]:
        """Ordered pool of eligible executors plus the decision reason.

        The first entry is the executor that should run; the rest are
        bounded fallbacks in preference order. Unhealthy executors may
        appear nowhere in the pool.
        """
        eligible = set(self.eligible_ids())
        health_by_id = {
            executor.id: self.health_service.health(executor)
            for executor in self.executors
        }

        def _rejection(executor_id: str) -> str:
            health = health_by_id.get(executor_id)
            if health is None:
                return "unknown executor"
            bits = ["unavailable", f"error_type={health.error_type or 'unknown'}"]
            if health.detail:
                bits.append(health.detail[:160])
            if health.circuit_open:
                bits.append("circuit open")
            return "; ".join(bits)

        # User override wins when healthy; falls back with the health
        # rejection recorded when not.
        if preference and preference != "auto":
            if preference in self._by_id and preference in eligible:
                chain = self._chain_from(preference, eligible, plan)
                return chain, f"user override accepted: {preference}"
            if preference in self._by_id:
                fallback = self._first_fallback(eligible, plan)
                if fallback:
                    return (
                        self._chain_from(fallback, eligible, plan),
                        f"user override {preference} rejected by health "
                        f"({_rejection(preference)}); fell back to {fallback}",
                    )
                return [], (
                    f"user override {preference} rejected by health and no "
                    f"eligible fallback exists"
                )
            fallback = self._first_fallback(eligible, plan)
            if not fallback:
                return [], f"user override {preference!r} is not a known executor"
            return (
                self._chain_from(fallback, eligible, plan),
                f"user override {preference!r} is not a known executor; "
                f"fell back to {fallback}",
            )

        chosen = plan.executor
        if chosen in eligible:
            return (
                self._chain_from(chosen, eligible, plan),
                f"planner selected {chosen}: {plan.reason}",
            )
        if chosen in self._by_id:
            fallback = self._first_fallback(eligible, plan)
            if fallback:
                return (
                    self._chain_from(fallback, eligible, plan),
                    f"planner selected {chosen} but health rejected it "
                    f"({_rejection(chosen)}); fell back to {fallback}",
                )
            return [], (
                f"planner selected {chosen} but health rejected it "
                f"({_rejection(chosen)}) and no eligible fallback exists"
            )
        fallback = self._first_fallback(eligible, plan)
        if not fallback:
            return [], "planner chose an unknown executor and nothing is eligible"
        return (
            self._chain_from(fallback, eligible, plan),
            f"planner selected unknown executor {chosen!r}; fell back to {fallback}",
        )

    # ------------------------------------------------------- helpers
    def _chain_from(self, preferred: str, eligible: set[str], plan: PlanResult) -> list[str]:
        """Preferred first, then task-type hint, then ordered backstop."""
        chain = [preferred]
        hinted = DEFAULT_BY_TASK_TYPE.get(plan.task_type.lower())
        for candidate in [hinted, *FALLBACK_ORDER]:
            if candidate and candidate in eligible and candidate not in chain:
                chain.append(candidate)
        return chain

    def _first_fallback(self, eligible: set[str], plan: PlanResult) -> Optional[str]:
        hinted = DEFAULT_BY_TASK_TYPE.get(plan.task_type.lower())
        if hinted and hinted in eligible:
            return hinted
        for candidate in FALLBACK_ORDER:
            if candidate in eligible:
                return candidate
        return None

    def _eligibility_summary(self) -> str:
        parts = []
        for executor in self.executors:
            health = self.health_service.health(executor)
            parts.append(
                f"{executor.id}: {'eligible' if health.eligible else 'ineligible'}"
                f"({health.error_type or 'ok'})"
            )
        return "; ".join(parts)


def default_registry() -> ExecutorRegistry:
    return ExecutorRegistry()
