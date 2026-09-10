"""Failure mining over benchmark evaluation results.

Consumes :class:`app.eval.models.EvaluationResult` records (the Eval core)
and extracts deterministic failure signals for the self-improving loop.

Poisoned-benchmark protection lives here: results whose evidence or
scores look tampered with are quarantined and never reach clustering.
Only clean ``FAIL_*`` results become signals; ``PASS`` and ``BLOCKED_*``
results are ignored for mining (they carry no failure to fix).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.eval.models import EvaluationResult, ResultClass

# Evidence keys that indicate a tampered/poisoned benchmark record.
POISON_KEY_SUBSTRINGS = (
    "override_score",
    "inject",
    "bypass",
    "ignore_regression",
    "disable_validation",
    "auto_approve",
    "poison",
)

MIN_SCORE = 0.0
MAX_SCORE = 100.0


@dataclass
class FailureSignal:
    """One clean, minable failure from a benchmark result."""

    case_id: str
    result_class: str
    failure_modes: list[str]
    score: float
    evidence_keys: list[str] = field(default_factory=list)

    @property
    def signature(self) -> str:
        """Deterministic grouping key for root-cause clustering."""
        modes = ",".join(sorted(self.failure_modes) or ["unknown"])
        return f"{self.result_class}:{modes}"


@dataclass
class QuarantinedResult:
    """A benchmark result excluded from mining, with reasons."""

    case_id: str
    reasons: list[str] = field(default_factory=list)


def _evidence_keys(evidence: Any) -> list[str]:
    if isinstance(evidence, dict):
        return [str(k) for k in evidence.keys()]
    return []


def detect_poisoned(result: EvaluationResult) -> list[str]:
    """Return quarantine reasons for a result, or [] when clean."""
    reasons: list[str] = []

    if result.score < MIN_SCORE or result.score > MAX_SCORE:
        reasons.append(f"score out of bounds: {result.score}")

    for key in _evidence_keys(result.evidence):
        lowered = key.lower()
        if any(token in lowered for token in POISON_KEY_SUBSTRINGS):
            reasons.append(f"suspicious evidence key: {key}")

    case_id = (result.case_id or "").lower()
    if "poison" in case_id:
        reasons.append("suspicious case id")

    return reasons


def sanitize_results(
    results: list[EvaluationResult],
) -> tuple[list[EvaluationResult], list[QuarantinedResult]]:
    """Split results into clean and quarantined (poisoned) sets."""
    clean: list[EvaluationResult] = []
    quarantined: list[QuarantinedResult] = []
    seen: dict[str, EvaluationResult] = {}

    for result in results:
        reasons = detect_poisoned(result)
        if reasons:
            quarantined.append(
                QuarantinedResult(case_id=result.case_id, reasons=reasons)
            )
            continue
        prior = seen.get(result.case_id)
        if prior is not None and prior.result_class != result.result_class:
            quarantined.append(
                QuarantinedResult(
                    case_id=result.case_id,
                    reasons=["conflicting duplicate case_id outcomes"],
                )
            )
            continue
        seen[result.case_id] = result
        clean.append(result)

    return clean, quarantined


def mine_failures(
    results: list[EvaluationResult],
) -> tuple[list[FailureSignal], list[QuarantinedResult]]:
    """Extract failure signals from clean ``FAIL_*`` results.

    Returns (signals, quarantined). PASS and BLOCKED results yield
    no signals. Poisoned results are quarantined, never mined.
    """
    clean, quarantined = sanitize_results(results)
    signals: list[FailureSignal] = []

    for result in clean:
        try:
            kind = ResultClass(result.result_class)
        except ValueError:
            quarantined.append(
                QuarantinedResult(
                    case_id=result.case_id,
                    reasons=[f"unknown result_class: {result.result_class}"],
                )
            )
            continue
        if not kind.name.startswith("FAIL"):
            continue
        modes = [
            str(m.value if hasattr(m, "value") else m)
            for m in (result.failure_modes or [])
        ]
        signals.append(
            FailureSignal(
                case_id=result.case_id,
                result_class=kind.value,
                failure_modes=sorted(set(modes)),
                score=float(result.score),
                evidence_keys=_evidence_keys(result.evidence),
            )
        )

    return signals, quarantined
