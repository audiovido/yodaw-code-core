"""Durable history store for routing feedback.

History only changes through explicit records. Corrupted rows are
skipped and reported, never applied silently.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PerformanceRecord:
    provider: str
    model: str
    success: bool
    latency_ms: float = 0.0
    case_id: str = ""
    score: float = 0.0

    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "success": self.success,
            "latency_ms": self.latency_ms,
            "case_id": self.case_id,
            "score": self.score,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PerformanceRecord":
        return cls(
            provider=str(data["provider"]),
            model=str(data["model"]),
            success=bool(data["success"]),
            latency_ms=float(data.get("latency_ms", 0.0)),
            case_id=str(data.get("case_id", "")),
            score=float(data.get("score", 0.0)),
        )


@dataclass
class ModelStats:
    provider: str
    model: str
    samples: int = 0
    successes: int = 0
    failures: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    avg_score: float = 0.0


class HistoryStore:
    """Explicit, file-backed performance history."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.records: list[PerformanceRecord] = []
        self.skipped: int = 0
        if self.path and self.path.exists():
            self.load()

    def record(self, entry: PerformanceRecord) -> None:
        self.records.append(entry)

    def record_result(
        self,
        provider: str,
        model: str,
        success: bool,
        latency_ms: float = 0.0,
        case_id: str = "",
        score: float = 0.0,
    ) -> PerformanceRecord:
        entry = PerformanceRecord(
            provider=provider,
            model=model,
            success=success,
            latency_ms=latency_ms,
            case_id=case_id,
            score=score,
        )
        self.record(entry)
        return entry

    def stats_for(self, provider: str, model: str) -> ModelStats | None:
        matching = [
            item
            for item in self.records
            if item.provider == provider and item.model == model
        ]
        if not matching:
            return None
        successes = sum(1 for item in matching if item.success)
        failures = len(matching) - successes
        return ModelStats(
            provider=provider,
            model=model,
            samples=len(matching),
            successes=successes,
            failures=failures,
            success_rate=successes / len(matching),
            avg_latency_ms=sum(item.latency_ms for item in matching)
            / len(matching),
            avg_score=sum(item.score for item in matching) / len(matching),
        )

    def recent_failures(
        self, provider: str, model: str, window: int = 5
    ) -> int:
        matching = [
            item
            for item in self.records
            if item.provider == provider and item.model == model
        ]
        return sum(1 for item in matching[-window:] if not item.success)

    def apply_to_registry(self, registry) -> int:
        """Fold durable records into registry counters explicitly."""
        applied = 0
        for entry in self.records:
            capability = registry.get(entry.provider, entry.model)
            if capability is None:
                continue
            if entry.success:
                capability.success_count += 1
            else:
                capability.failure_count += 1
            applied += 1
        return applied

    def load(self) -> None:
        assert self.path is not None
        raw = json.loads(self.path.read_text())
        rows = raw.get("records", raw) if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            self.skipped += 1
            return
        for row in rows:
            try:
                self.records.append(PerformanceRecord.from_dict(row))
            except (KeyError, TypeError, ValueError):
                self.skipped += 1

    def save(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "records": [item.to_dict() for item in self.records],
        }
        self.path.write_text(json.dumps(payload, indent=2))

    @classmethod
    def from_eval_report(cls, report: dict) -> "HistoryStore":
        """Build history from one benchmark report dict."""
        store = cls()
        cases = report.get("cases", [])
        for case in cases:
            evidence = case.get("evidence", {}) or {}
            routing = evidence.get("routing", {}) or {}
            provider = routing.get("provider", "")
            model = routing.get("model", "")
            if not provider or not model:
                continue
            result = str(case.get("result_class", ""))
            success = result == "PASS"
            store.record_result(
                provider=provider,
                model=model,
                success=success,
                latency_ms=float(
                    (case.get("performance", {}) or {}).get(
                        "wall_clock_time", 0.0
                    )
                )
                * 1000.0,
                case_id=str(case.get("case_id", "")),
                score=float(case.get("score", 0.0)),
            )
        return store
