"""
Hypothesis-Driven Debugging Engine.

For failures: collect evidence, generate competing hypotheses, rank
by probability/cost, run the cheapest discriminating test, update
confidence, repeat. Records hypothesis / evidence / experiment /
result / next decision.

This replaces random-edit debugging with a structured Bayesian loop.
The loop is pure and hermetic: hypothesis generation can be fed by
keywords or by an injected model; ranking and selection are
deterministic.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

MAX_HYPOTHESES = 8


@dataclass
class Hypothesis:
    """One competing explanation for a failure."""

    id: str
    description: str
    confidence: float = 0.3
    cost: float = 1.0          # relative cost of the discriminating experiment
    evidence_for: List[str] = field(default_factory=list)
    evidence_against: List[str] = field(default_factory=list)
    status: str = "pending"    # pending | active | confirmed | eliminated

    def score(self) -> float:
        """Ranking: probability of being right per unit experiment cost."""
        if self.status == "eliminated":
            return 0.0
        return self.confidence / max(self.cost, 0.1)


@dataclass
class DebugRecord:
    """One hypothesis round, recorded for evidence and learning."""

    round: int
    failure_snippet: str
    hypothesis: Hypothesis
    experiment: str = ""
    result: str = ""
    next_decision: str = ""


@dataclass
class DebugSession:
    """Accumulated state of one debugging session."""

    goal: str
    failure_snippet: str
    hypotheses: List[Hypothesis] = field(default_factory=list)
    records: List[DebugRecord] = field(default_factory=list)
    rounds: int = 0
    concluded: bool = False
    resolution: str = ""

    def record(self, record: DebugRecord) -> None:
        self.records.append(record)
        self.rounds = len(self.records)

    def summary(self) -> dict:
        return {
            "rounds": self.rounds,
            "concluded": self.concluded,
            "resolution": self.resolution,
            "hypotheses": [
                {
                    "id": h.id,
                    "description": h.description,
                    "confidence": round(h.confidence, 3),
                    "status": h.status,
                }
                for h in self.hypotheses
            ],
            "records": [
                {
                    "round": r.round,
                    "hypothesis": r.hypothesis.id,
                    "experiment": r.experiment,
                    "result": r.result,
                    "next_decision": r.next_decision,
                }
                for r in self.records
            ],
        }


# Keyword -> candidate root causes (cheap, deterministic generation).
FAILURE_SIGNALS: List[tuple] = [
    (r"race|deadlock|lock|concurr|thread|atomic", "concurrency defect", "concurrency"),
    (r"null|none|keyerror|attributeerror|undefined|nullable", "null/undefined access", "nullness"),
    (r"timeout|timed out|slow|hang", "timeout / hang", "timing"),
    (r"memory|leak|oom|segfault|overflow", "memory error", "memory"),
    (r"import|module|require.*not found|cannot find module", "import/dependency resolution", "imports"),
    (r"permission|denied|unauthorized|forbidden|auth", "permission/authz", "authz"),
    (r"tenant|isolation|scope record|multi.?tenant", "tenant isolation", "tenancy"),
    (r"json|parse|decode|encoding|utf", "serialization mismatch", "serialization"),
    (r"schema|migration|column|constraint|sqlite", "schema/data state", "schema"),
    (r"timezone|utc|offset|timestamp|clock", "clock/timezone handling", "timezone"),
    (r"cancel|deadline|ctx\b", "cancellation/deadline propagation", "cancellation"),
    (r"flaky|intermittent|sometimes|random", "flaky ordering/timing", "flakiness"),
]


def infer_root_cause(snippet: str) -> List[str]:
    """Map failure text to candidate root-cause categories."""
    causes: List[str] = []
    lower = snippet.lower()
    for pattern, _, category in FAILURE_SIGNALS:
        if re.search(pattern, lower):
            if category not in causes:
                causes.append(category)
    if not causes:
        causes.append("logic")
    return causes


def generate_hypotheses(
    failure_snippet: str,
    goal: str = "",
    model_fn: Optional[Callable[[str], str]] = None,
) -> List[Hypothesis]:
    """Generate competing hypotheses. Model-backed if provided,
    otherwise deterministic keyword-based generation."""
    categories = infer_root_cause(failure_snippet)
    hyps: List[Hypothesis] = []
    base_id = f"h{int(time.time() * 1000) % 100000}"
    for i, cat in enumerate(categories[:MAX_HYPOTHESES]):
        hyps.append(
            Hypothesis(
                id=f"{base_id}_{i}",
                description=f"root cause: {cat}",
                confidence=0.35,
                cost=1.0 + 0.25 * i,
            )
        )

    if model_fn is not None:
        try:
            raw = model_fn(
                "List up to 5 likely root causes for this failure as "
                "newline-separated short phrases, ranked by likelihood:\n\n"
                f"{failure_snippet[:4000]}"
            )
            for i, line in enumerate(
                [l.strip("- *") for l in raw.splitlines() if l.strip()][:5]
            ):
                hyps.append(
                    Hypothesis(
                        id=f"{base_id}_m{i}",
                        description=line[:200],
                        confidence=max(0.3, 0.5 - 0.05 * i),
                        cost=1.5,
                    )
                )
        except Exception:
            pass

    return hyps[:MAX_HYPOTHESES]


def rank_hypotheses(hypotheses: List[Hypothesis]) -> List[Hypothesis]:
    """Rank by score (confidence per unit cost); eliminates first."""
    return sorted(hypotheses, key=lambda h: h.score(), reverse=True)


def cheapest_discriminating(hypotheses: List[Hypothesis]) -> Optional[Hypothesis]:
    """Pick the highest-ranked non-terminated hypothesis."""
    ranked = rank_hypotheses(hypotheses)
    return next((h for h in ranked if h.status == "pending"), None)


def apply_experiment_result(
    session: DebugSession,
    hypothesis_id: str,
    succeeded: Optional[bool],
    experiment_desc: str,
) -> None:
    """Update confidence and status after a discriminating experiment.

    succeeded=True  -> boosts confidence (experiment confirmed cause)
    succeeded=False -> eliminates the hypothesis
    succeeded=None  -> inconclusive; keep pending
    """
    hyp = next((h for h in session.hypotheses if h.id == hypothesis_id), None)
    if hyp is None:
        return
    record = DebugRecord(
        round=session.rounds + 1,
        failure_snippet=session.failure_snippet[:2000],
        hypothesis=hyp,
        experiment=experiment_desc,
    )
    if succeeded is True:
        hyp.confidence = min(hyp.confidence * 1.8 + 0.15, 0.97)
        hyp.status = "confirmed" if hyp.confidence > 0.7 else hyp.status
        record.result = "confirmed"
        record.next_decision = "implement fix for this hypothesis"
        session.concluded = True
        session.resolution = hyp.description
    elif succeeded is False:
        hyp.status = "eliminated"
        hyp.confidence = 0.0
        record.result = "eliminated"
        remaining = [h for h in session.hypotheses if h.status == "pending"]
        record.next_decision = (
            f"test next hypothesis ({remaining[0].id})" if remaining else "no hypotheses left; re-investigate"
        )
        if not remaining:
            session.concluded = True
            session.resolution = "no surviving hypothesis; collect more evidence"
    else:
        record.result = "inconclusive"
        record.next_decision = "gather more evidence"
    hyp.evidence_for.append(experiment_desc) if succeeded is True else None
    if succeeded is False:
        hyp.evidence_against.append(experiment_desc)
    session.record(record)


def format_debug_context(session: DebugSession) -> str:
    """Render the debugging session for the Coder Brain prompt."""
    parts = ["DEBUGGING SESSION (hypothesis-driven):"]
    for h in rank_hypotheses(session.hypotheses):
        parts.append(
            f"  [{h.id}] {h.description} "
            f"(confidence {h.confidence:.2f}, cost {h.cost:.1f}, status {h.status})"
        )
    if session.records:
        parts.append("  EXPERIMENT HISTORY:")
        for r in session.records:
            parts.append(
                f"    round {r.round}: {r.hypothesis.id} / {r.experiment[:120]} -> {r.result}"
            )
    return "\n".join(parts)