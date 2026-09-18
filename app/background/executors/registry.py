"""Executor registry and V1 selection policy.

The registry is the only place that knows which coding agents exist on
the machine. Two rules shape it:

- **Availability is probed, never assumed.** Every adapter reports the
  real result of looking for its binary on PATH.
- **Absence degrades, it does not fail.** A missing executor is
  reported as unavailable and the planner/router simply cannot select
  it; the product keeps working with the rest.

V1 selection is deliberately simple and inspectable — no learned
router yet. The planner (Grok) picks an executor and returns its
reasoning; the router here enforces that the choice is *possible*
(fallback to a live executor if the plan named a missing one) and
records exactly why the final choice was made.
"""

from __future__ import annotations

from typing import Optional

from app.background.executors.base import Executor
from app.background.executors.claude_code import ClaudeCodeExecutor
from app.background.executors.codex import CodexExecutor
from app.background.executors.grok_cli import GrokCliExecutor
from app.background.executors.kodgar_native import KodgarNativeExecutor
from app.background.models import ExecutorInfo, PlanResult

# Ordered fallbacks: when the planner's executor is unavailable, the
# first *available* entry here takes over. Native is last because it
# depends only on the project's own engine, so it is always a valid
# backstop.
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


class ExecutorRegistry:
    """All known executors plus the routing decision."""

    def __init__(self, executors: Optional[list[Executor]] = None):
        self.executors: list[Executor] = executors or [
            ClaudeCodeExecutor(),
            CodexExecutor(),
            GrokCliExecutor(),
            KodgarNativeExecutor(),
        ]
        self._by_id = {executor.id: executor for executor in self.executors}

    # ---------------------------------------------------------- read
    def get(self, executor_id: str) -> Optional[Executor]:
        return self._by_id.get(executor_id)

    def infos(self) -> list[ExecutorInfo]:
        return [executor.info() for executor in self.executors]

    def availability(self) -> dict[str, ExecutorInfo]:
        return {info.id: info for info in self.infos()}

    def available_ids(self) -> list[str]:
        return [info.id for info in self.infos() if info.available]

    # -------------------------------------------------------- select
    def select(
        self,
        plan: PlanResult,
        preference: Optional[str] = None,
    ) -> tuple[str, str]:
        """Resolve ``(executor_id, reason)`` for a validated plan.

        ``preference`` is a user override (``preferences.executor``).
        An override that names an unavailable executor falls back with
        a reason that says so — it never silently pretends to run.
        """
        available = set(self.available_ids())

        if preference and preference != "auto":
            if preference in self._by_id and preference in available:
                return preference, f"user override accepted: {preference}"
            if preference in self._by_id:
                info = self._by_id[preference].info()
                fallback = self._fallback(available, plan)
                return (
                    fallback,
                    f"user override {preference} unavailable "
                    f"({info.detail}); fell back to {fallback}",
                )
            fallback = self._fallback(available, plan)
            return (
                fallback,
                f"user override {preference!r} is not a known executor; "
                f"fell back to {fallback}",
            )

        chosen = plan.executor
        if chosen in available:
            return chosen, f"planner selected {chosen}: {plan.reason}"

        detail = ""
        if chosen in self._by_id:
            detail = self._by_id[chosen].info().detail
        fallback = self._fallback(available, plan)
        return (
            fallback,
            f"planner selected {chosen} but it is unavailable"
            f"{f' ({detail})' if detail else ''}; fell back to {fallback}",
        )

    def _fallback(self, available: set[str], plan: PlanResult) -> str:
        # Planner's task_type hint first, then the ordered backstop.
        hinted = DEFAULT_BY_TASK_TYPE.get(plan.task_type.lower())
        if hinted and hinted in available:
            return hinted
        for candidate in FALLBACK_ORDER:
            if candidate in available:
                return candidate
        raise RuntimeError("no executor is available on this machine")

    # --------------------------------------------------------- status
    def status(self) -> dict:
        infos = self.infos()
        return {
            "executors": [info.model_dump() for info in infos],
            "available": [info.id for info in infos if info.available],
            "unavailable": [info.id for info in infos if not info.available],
        }


def default_registry() -> ExecutorRegistry:
    return ExecutorRegistry()
