"""
Stage 10.4: per-client rate limiting and payload governance.

Rate limiting:

- token bucket per client, interval-scaled (requests per minute)
- state persisted through a store-agnostic backend table, so the
  limit holds under multi-process deployment (two coordinators /
  multiple uvicorn workers share the database-backed bucket)
- process-local fast path guarded by a lock; the atomic check-
  and-debit happens in the backend transaction
- 429 responses carry Retry-After and X-RateLimit-* headers
- limit failures are audited by the caller

Payload governance (checked BEFORE any expensive processing):

- max request body size
- max goal length
- max metadata serialized size
- mission creation burst limit (separate from the steady RPS
  bucket) and per-client mission creation rate

Governance limits support global defaults plus per-client
overrides stored on the client identity.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class RateLimitConfig:
    """Resolved rate-limit configuration for one client."""

    requests_per_minute: int = 60
    burst: int = 10
    missions_per_minute: int = 10


@dataclass
class GovernanceConfig:
    """Global payload governance defaults."""

    max_body_bytes: int = 256 * 1024
    max_goal_length: int = 4000
    max_metadata_bytes: int = 32 * 1024
    max_evidence_response_bytes: int = 512 * 1024


DEFAULT_RATE = RateLimitConfig()
DEFAULT_GOVERNANCE = GovernanceConfig()


class RateLimiter:
    """
    Token bucket over a persistence backend.

    The backend contract (both SQLite and Postgres adapters
    implement it):

        take(client_key, now, capacity, refill_rate, tokens) -> (ok, retry_after)

    `client_key` may be a client id, admin id, or the literal
    "shared-key" / "local-dev" principal. The backend row is
    updated atomically, so multiple processes share one bucket.
    """

    def __init__(
        self,
        backend,
        clock=None,
        config: RateLimitConfig | None = None,
    ):
        self.backend = backend
        self.clock = clock or (
            lambda: __import__("time").time()
        )
        self.config = config or RateLimitConfig()
        self._lock = threading.Lock()

    def check(
        self,
        client_key: str,
        *,
        tokens: float = 1.0,
        rpm: int | None = None,
        burst: int | None = None,
    ) -> tuple[bool, float]:
        """
        Try to take tokens from the client's bucket.

        Returns (allowed, retry_after_seconds). retry_after is 0.0
        when allowed.
        """
        cfg = self.config
        rpm = cfg.requests_per_minute if rpm is None else rpm
        burst = cfg.burst if burst is None else burst

        capacity = max(1, max(burst, 1))
        refill_per_second = max(1, rpm) / 60.0
        now = self.clock()

        with self._lock:
            return self.backend.rate_limit_take(
                client_key,
                now=now,
                capacity=capacity,
                refill_per_second=refill_per_second,
                tokens=tokens,
            )


def check_payload_governance(
    *,
    goal: str,
    metadata: dict | None,
    config: GovernanceConfig | None = None,
) -> tuple[bool, str]:
    """
    Pre-execution payload validation.

    Returns (ok, reason). Reason is empty when ok.
    """
    cfg = config or DEFAULT_GOVERNANCE

    if len(goal) > cfg.max_goal_length:
        return False, (
            f"goal exceeds maximum length "
            f"({len(goal)} > {cfg.max_goal_length})"
        )

    if metadata:
        import json

        try:
            size = len(json.dumps(metadata))
        except (TypeError, ValueError):
            return False, "metadata is not JSON-serializable"

        if size > cfg.max_metadata_bytes:
            return False, (
                f"metadata exceeds maximum serialized size "
                f"({size} > {cfg.max_metadata_bytes} bytes)"
            )

    return True, ""
