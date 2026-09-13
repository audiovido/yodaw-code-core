"""Structured mission evidence: deterministic, bounded, PASS/FAIL-compatible.

Every event is a dict with a stable key order (type, timestamp, then
data) and a bounded payload, so serialized evidence is deterministic
for identical inputs and can never grow unbounded. PASS/FAIL summaries
use a fixed schema consumed by mission reporting.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

MAX_EVENT_BYTES = 64_000
MAX_EVIDENCE_EVENTS = 1_000

_EVENT_KEY_ORDER = ("type", "timestamp")


def now_iso() -> str:
    """Current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def make_event(event_type: str, timestamp: Optional[str] = None, **data: Any) -> dict:
    """Build a canonical evidence event with a fixed key order."""
    event: dict[str, Any] = {"type": event_type}
    event["timestamp"] = timestamp or now_iso()
    for key, value in data.items():
        event[key] = value
    return event


def clamp_event(event: dict, max_bytes: int = MAX_EVENT_BYTES) -> dict:
    """Truncate an event payload to max_bytes while keeping type/timestamp.

    The truncation is recorded on the event itself (truncated +
    truncated_bytes) so evidence never silently loses data.
    """
    if _estimate_bytes(event) <= max_bytes:
        return event

    cloned: dict[str, Any] = {
        "type": event.get("type"),
        "timestamp": event.get("timestamp"),
    }

    for key, value in event.items():
        if key in _EVENT_KEY_ORDER:
            continue

        candidate = dict(cloned)
        candidate[key] = value
        if _estimate_bytes(candidate) <= max_bytes:
            cloned[key] = value
            continue

        remaining = max_bytes - _estimate_bytes(cloned) - 128
        if remaining > 0 and isinstance(value, str):
            cloned[key] = value[:remaining]
        break

    truncated_bytes = max(_estimate_bytes(event) - _estimate_bytes(cloned), 0)
    cloned["truncated"] = True
    cloned["truncated_bytes"] = truncated_bytes
    return cloned


def accumulate(evidence: list, event: dict) -> list:
    """Append a (clamped) event, enforcing a hard cap on list length."""
    bounded = clamp_event(event)
    evidence.append(bounded)
    if len(evidence) > MAX_EVIDENCE_EVENTS:
        dropped = len(evidence) - MAX_EVIDENCE_EVENTS
        del evidence[:dropped]
        evidence.append(make_event("evidence_truncated", dropped=dropped))
    return evidence


def provider_attempt_evidence() -> list:
    """Surface pending provider attempts as evidence (empty when none)."""
    try:
        from app.llm.provider import pop_attempt_log

        attempts = pop_attempt_log()
    except Exception:
        return []

    if not attempts:
        return []

    return [
        make_event(
            "provider_attempts",
            attempts=attempts,
        )
    ]


def pass_fail_summary(results, *, required_conditions=None) -> dict:
    """Reduce per-check results into a deterministic PASS/FAIL schema.

    Checks are reported in input order; an empty check list passes
    (nothing to fail), mirroring worker no-test semantics.
    """
    checks = []
    for index, result in enumerate(results):
        if isinstance(result, dict):
            name = result.get("name", str(result.get("cmd", index)))
            ok = bool(result.get("returncode") == 0 and not result.get("timed_out", False))
            checks.append(
                {
                    "name": name,
                    "ok": ok,
                    "detail": result.get("stderr", "")[:400]
                    if not ok
                    else "",
                }
            )
        else:
            ok = bool(result)
            checks.append({"name": str(index), "ok": ok, "detail": ""})

    if required_conditions is not None:
        for name, ok in required_conditions.items():
            checks.insert(0, {"name": name, "ok": bool(ok), "detail": ""})

    passed = sum(1 for check in checks if check["ok"])
    failed = len(checks) - passed
    return {
        "status": "PASS" if failed == 0 else "FAIL",
        "checks": checks,
        "passed": passed,
        "failed": failed,
        "total": len(checks),
    }


def _estimate_bytes(value: Any) -> int:
    return len(json.dumps(value, default=str))