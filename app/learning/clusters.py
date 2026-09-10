"""Root-cause clustering over mined failure signals.

Groups :class:`FailureSignal` records by their deterministic signature
(``result_class`` + sorted failure modes, see
``app.learning.failure_miner``). Only recurring groups — at least
``min_occurrences`` signals — become clusters. Singletons and pairs
below the threshold are reported as ignored so callers can prove
unique failures never seed improvements.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.learning.failure_miner import FailureSignal

DEFAULT_MIN_OCCURRENCES = 3


@dataclass
class FailureCluster:
    """One recurring root-cause group."""

    id: str
    signature: str
    result_class: str
    failure_modes: list[str]
    size: int
    case_ids: list[str] = field(default_factory=list)
    avg_score: float = 0.0


def cluster_id_for(signature: str) -> str:
    """Deterministic cluster id derived from the signature."""
    digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()
    return f"fc_{digest[:12]}"


def cluster_signals(
    signals: list[FailureSignal],
    min_occurrences: int = DEFAULT_MIN_OCCURRENCES,
) -> tuple[list[FailureCluster], list[FailureSignal]]:
    """Group signals into recurring clusters.

    Returns (clusters, ignored). Clusters are sorted by descending
    size then signature for deterministic iteration. ``ignored``
    holds every signal that did not reach the recurrence threshold.
    """
    grouped: dict[str, list[FailureSignal]] = {}
    for signal in signals:
        grouped.setdefault(signal.signature, []).append(signal)

    clusters: list[FailureCluster] = []
    ignored: list[FailureSignal] = []

    for signature, members in grouped.items():
        if len(members) < max(2, int(min_occurrences)):
            ignored.extend(members)
            continue
        ordered = sorted(members, key=lambda s: s.case_id)
        avg = sum(s.score for s in ordered) / len(ordered)
        clusters.append(
            FailureCluster(
                id=cluster_id_for(signature),
                signature=signature,
                result_class=ordered[0].result_class,
                failure_modes=sorted(
                    {m for s in ordered for m in s.failure_modes}
                ),
                size=len(ordered),
                case_ids=[s.case_id for s in ordered],
                avg_score=avg,
            )
        )

    clusters.sort(key=lambda c: (-c.size, c.signature))
    ignored.sort(key=lambda s: s.case_id)
    return clusters, ignored
