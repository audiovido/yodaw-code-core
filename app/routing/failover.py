"""Bounded failover across the routing fallback chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FailureKind(str, Enum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    AUTH_UNAVAILABLE = "auth_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    DETERMINISTIC = "deterministic"


class RoutingExhausted(RuntimeError):
    """All eligible fallbacks failed; caller maps to BLOCKED_EXTERNAL."""

    def __init__(self, attempts: list[dict]):
        super().__init__(
            f"all {len(attempts)} routing fallback(s) failed"
        )
        self.attempts = attempts


@dataclass
class FailoverResult:
    selected: str
    attempts: list[dict] = field(default_factory=list)
    output: object = None


def classify_failure(exc: Exception) -> FailureKind:
    """Classify an exception without importing provider internals."""
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status == 429:
        return FailureKind.RATE_LIMITED
    if isinstance(status, int) and status >= 500:
        return FailureKind.SERVER_ERROR
    if status in (401, 403):
        return FailureKind.AUTH_UNAVAILABLE
    if "timeout" in name or "timeout" in message or "timed out" in message:
        return FailureKind.TIMEOUT
    if "connect" in name or "connection" in message or "unavailable" in message:
        return FailureKind.PROVIDER_UNAVAILABLE
    if "auth" in message and ("missing" in message or "invalid" in message):
        return FailureKind.AUTH_UNAVAILABLE
    if "429" in message:
        return FailureKind.RATE_LIMITED
    if "503" in message or "500" in message or "502" in message:
        return FailureKind.SERVER_ERROR
    return FailureKind.DETERMINISTIC


def is_retryable_kind(kind: FailureKind) -> bool:
    return kind in (
        FailureKind.TIMEOUT,
        FailureKind.RATE_LIMITED,
        FailureKind.SERVER_ERROR,
        FailureKind.PROVIDER_UNAVAILABLE,
        FailureKind.AUTH_UNAVAILABLE,
    )


def execute_with_failover(
    chain: list[str],
    call,
    on_attempt=None,
    max_attempts: int | None = None,
) -> FailoverResult:
    """Walk a bounded fallback chain until one backend succeeds.

    The callable receives one chain key per attempt. Retryable
    failures advance to the next key; deterministic failures stop
    immediately. Exhaustion raises RoutingExhausted so the caller
    can record BLOCKED_EXTERNAL exactly once.
    """
    if not chain:
        raise RoutingExhausted([])
    bound = max_attempts if max_attempts is not None else len(chain)
    attempts: list[dict] = []
    for key in chain[:bound]:
        try:
            output = call(key)
        except Exception as exc:
            kind = classify_failure(exc)
            record = {
                "selected": key,
                "ok": False,
                "failure": kind.value,
                "error": f"{type(exc).__name__}: {exc}",
            }
            attempts.append(record)
            if on_attempt is not None:
                on_attempt(record)
            if not is_retryable_kind(kind):
                raise
            continue
        record = {"selected": key, "ok": True}
        attempts.append(record)
        if on_attempt is not None:
            on_attempt(record)
        return FailoverResult(selected=key, attempts=attempts, output=output)
    raise RoutingExhausted(attempts)
