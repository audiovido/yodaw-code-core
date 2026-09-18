"""Executor health: real evidence instead of binary detection.

``which claude`` says nothing about whether Claude can actually serve a
completion. This module turns each executor into a *ladder* of facts:

    INSTALLED -> AUTHENTICATED -> MODEL_AVAILABLE -> REAL_INFERENCE_OK

A rung that cannot be verified stays ``None`` (unknown), never silently
``True``. A rung that fails with a fatal provider error (quota, 403,
unknown model, auth) marks the executor ineligible for routing and, on
repeat failure, opens a circuit with a bounded cooldown so live traffic
is not repeatedly fed into a known-broken executor.

Probes are bounded (no probe may hang a request), cached with a TTL,
and can be forced to re-check. A *live* execution failure feeds back
into the same circuit breaker through ``record_failure``/
``record_success`` — the probe result and the battlefield result must
agree.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from typing import Any, Optional

from app.background.models import (
    FATAL_EXECUTOR_ERRORS,
    ExecutorHealth,
    now_iso,
)

DEFAULT_PROBE_TIMEOUT = 45.0
DEFAULT_TTL_SECONDS = 120.0
DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_FAILURE_THRESHOLD = 2

# Error classification: structured evidence first, patterns as fallback.
_MODEL_NOT_FOUND = re.compile(
    r"model[_ -]?(not[_ -]?found|does not exist|unknown model)|"
    r"no such model|invalid model",
    re.IGNORECASE,
)
_QUOTA = re.compile(
    r"quota|rate limit|MONTHLY_REQUEST_COUNT|billing|402",
    re.IGNORECASE,
)
_FORBIDDEN = re.compile(
    r"forbidden|403|FreeTierError|not allowed|permission denied",
    re.IGNORECASE,
)
_AUTH = re.compile(
    r"unauthorized|401|invalid api key|authentication|login required",
    re.IGNORECASE,
)
_TIMEOUTISH = re.compile(r"timed? ?out|etimedout|ereadtimeout", re.IGNORECASE)
_OVERLOAD = re.compile(r"overloaded|503|service unavailable|temporarily", re.IGNORECASE)
# The executor worked, but the *plan* could not be applied to the tree
# (an edit anchor that does not match, an impossible file edit). That is
# a task/plan defect, not executor ill-health, so it must not send the
# task to a different executor or count against the executor.
_EDIT_NOT_APPLICABLE = re.compile(
    r"source text was not found|find text|findtextmissing|"
    r"exact or unique relaxed match|target file .* not found",
    re.IGNORECASE,
)


def classify_executor_error(message: str, status: Optional[int] = None) -> str:
    """Map provider/CLI evidence to one of the retry classes.

    Structured evidence (an HTTP status from the gateway) wins over
    string patterns; patterns exist because CLI tools bury the status
    in prose. Unknown text conservatively maps to
    ``RETRY_DIFFERENT_EXECUTOR`` — trying another executor is always
    safe; retrying a mystery failure on the same one may not be.
    """
    text = message or ""
    if status == 404 or _MODEL_NOT_FOUND.search(text):
        return "model_not_found"
    if status == 401 or _AUTH.search(text):
        return "auth_failed"
    if status == 402 or status == 429 or _QUOTA.search(text):
        return "quota_exhausted"
    if status == 403 or _FORBIDDEN.search(text):
        return "upstream_forbidden"
    if _TIMEOUTISH.search(text):
        return "transient_timeout"
    if status is not None and 500 <= status < 600:
        return "transient_overload"
    if _OVERLOAD.search(text):
        return "transient_overload"
    # Checked after the infrastructure classes: a gateway 403 that
    # happens to mention files must still classify as upstream_forbidden.
    if _EDIT_NOT_APPLICABLE.search(text):
        return "edit_not_applicable"
    return "unknown"


def error_class_action(error_type: str) -> str:
    """The routing decision for a classified error."""
    if error_type in {"transient_timeout", "transient_overload"}:
        return "RETRY_SAME_EXECUTOR"
    if error_type in FATAL_EXECUTOR_ERRORS or error_type == "unknown":
        return "RETRY_DIFFERENT_EXECUTOR"
    # edit_not_applicable and every other class is a task-side defect:
    # report it, do not cycle executors, do not blame the executor.
    return "NON_RETRYABLE_TASK_ERROR"


class _CircuitState:
    __slots__ = ("failures", "open_until")

    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0


class ExecutorHealthService:
    """TTL-cached health probes + circuit breaker, shared process-wide."""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
    ):
        self.ttl_seconds = ttl_seconds
        self.cooldown_seconds = cooldown_seconds
        self.failure_threshold = max(1, failure_threshold)
        self.probe_timeout = probe_timeout
        self._cache: dict[str, ExecutorHealth] = {}
        self._circuits: dict[str, _CircuitState] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ probe
    def health(self, executor, force: bool = False) -> ExecutorHealth:
        """Health for one executor adapter, cached for ``ttl_seconds``."""
        executor_id = executor.id
        now = time.monotonic()
        with self._lock:
            circuit = self._circuits.setdefault(executor_id, _CircuitState())
            cached = self._cache.get(executor_id)
            if (
                not force
                and cached is not None
                and now - _parse_ts(cached.checked_at) < self.ttl_seconds
            ):
                return self._with_circuit(cached, circuit)
            if now < circuit.open_until:
                # Circuit open: report known-bad without probing.
                if cached is not None and cached.error_type in FATAL_EXECUTOR_ERRORS:
                    return self._with_circuit(cached, circuit)
                breakers = self._synthetic_breaker(executor, circuit)
                return self._with_circuit(breakers, circuit)

        health = self._probe(executor)
        with self._lock:
            self._cache[executor_id] = health
            circuit = self._circuits.setdefault(executor_id, _CircuitState())
            was_open = now < circuit.open_until
            if health.error_type in FATAL_EXECUTOR_ERRORS:
                circuit.failures += 1
                if circuit.failures >= self.failure_threshold:
                    circuit.open_until = time.monotonic() + self.cooldown_seconds
            elif health.healthy and was_open:
                # Recovery: the cooldown elapsed and the probe now
                # succeeds — clear the breaker state.
                circuit.failures = 0
                circuit.open_until = 0.0
            # A healthy probe while NO circuit is open must NOT erase
            # live-failure counts: the probe can be fine while real
            # executions fail (quota, model 404). Only a live success
            # (record_success) or cooldown expiry clears those.
        return self._with_circuit(health, self._circuits[executor_id])

    def record_failure(self, executor_id: str, message: str, status: Optional[int] = None) -> str:
        """Feed a *live execution* failure into the breaker.

        Returns the classified error type. Fatal classes open the
        circuit after ``failure_threshold`` consecutive failures;
        transient classes only clear the cache so the next probe
        re-checks.
        """
        error_type = classify_executor_error(message, status)
        with self._lock:
            circuit = self._circuits.setdefault(executor_id, _CircuitState())
            self._cache.pop(executor_id, None)
            if error_type in FATAL_EXECUTOR_ERRORS:
                circuit.failures += 1
                if circuit.failures >= self.failure_threshold:
                    circuit.open_until = time.monotonic() + self.cooldown_seconds
        return error_type

    def record_success(self, executor_id: str) -> None:
        with self._lock:
            circuit = self._circuits.setdefault(executor_id, _CircuitState())
            circuit.failures = 0
            circuit.open_until = 0.0
            self._cache.pop(executor_id, None)

    # --------------------------------------------------------- reading
    def eligible_ids(self, executors: list) -> list[str]:
        """Executor ids that may receive work right now."""
        eligible: list[str] = []
        for executor in executors:
            health = self.health(executor)
            if health.eligible:
                eligible.append(executor.id)
        return eligible

    def status(self, executors: list) -> dict:
        healths = [self.health(executor) for executor in executors]
        return {
            "executors": [health.model_dump() for health in healths],
            "available": [h.id for h in healths if h.eligible],
            "unavailable": [h.id for h in healths if not h.eligible],
            "healthy": [h.id for h in healths if h.healthy],
        }

    # ------------------------------------------------------- internals
    def _with_circuit(self, health: ExecutorHealth, circuit: _CircuitState) -> ExecutorHealth:
        open_now = time.monotonic() < circuit.open_until
        health.circuit_open = open_now
        if open_now:
            health.eligible = False
            remaining = int(circuit.open_until - time.monotonic())
            health.cooldown_until = now_iso() if remaining <= 0 else _iso_in(remaining)
            if not health.error_type:
                health.error_type = "circuit_open"
                health.detail = health.detail or "circuit open after repeated failures"
        else:
            health.cooldown_until = None
        return health

    def _synthetic_breaker(self, executor, circuit: _CircuitState) -> ExecutorHealth:
        return ExecutorHealth(
            id=executor.id,
            label=getattr(executor, "label", executor.id),
            kind=getattr(executor, "kind", "cli"),
            installed=True,
            error_type="circuit_open",
            detail="circuit open after repeated fatal failures; re-probe after cooldown",
            capabilities=list(getattr(executor, "capabilities", ()) or ()),
        )

    def _probe(self, executor) -> ExecutorHealth:
        kind = getattr(executor, "kind", "cli")
        started = time.monotonic()
        try:
            if kind == "native":
                health = _probe_native(executor)
            elif executor.id == "claude-code":
                health = _probe_claude(executor, self.probe_timeout)
            elif executor.id == "grok-cli":
                health = _probe_grok(executor, self.probe_timeout)
            elif executor.id == "codex":
                health = _probe_codex(executor, self.probe_timeout)
            else:  # pragma: no cover - unknown adapter kind
                installed, detail = executor.available()
                health = ExecutorHealth(
                    id=executor.id,
                    label=getattr(executor, "label", executor.id),
                    kind=kind,
                    installed=installed,
                    healthy=installed,
                    eligible=installed,
                    detail=detail,
                )
        except Exception as exc:  # a probe must never raise outward
            health = ExecutorHealth(
                id=executor.id,
                label=getattr(executor, "label", executor.id),
                kind=kind,
                installed=False,
                error_type="probe_error",
                detail=f"{type(exc).__name__}: {exc}",
            )
        health.latency_ms = int((time.monotonic() - started) * 1000)
        health.checked_at = now_iso()
        if health.healthy and not health.error_type:
            health.eligible = True
        return health


# --------------------------------------------------------------- probes
def _probe_native(executor) -> ExecutorHealth:
    installed, detail = executor.available()
    return ExecutorHealth(
        id=executor.id,
        label=executor.label,
        kind=executor.kind,
        installed=installed,
        authenticated=True,
        model_available=True,
        inference_ok=True,
        healthy=installed,
        detail=detail,
        version=executor.version(),
        capabilities=list(executor.capabilities),
    )


def _run_bounded(cmd: list[str], timeout: float, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )


def _probe_claude(executor, timeout: float) -> ExecutorHealth:
    health = ExecutorHealth(id=executor.id, label=executor.label, kind=executor.kind)
    installed, detail = executor.available()
    health.installed = installed
    health.detail = detail
    if not installed:
        health.error_type = "not_installed"
        return health
    health.version = executor.version()
    # 1-token real inference through the configured provider. Every
    # failure mode the gateway can produce surfaces here with its real
    # status/text, which classify_executor_error maps to a class.
    try:
        result = _run_bounded(
            ["claude", "-p", "Reply with exactly: OK", "--output-format", "text",
             "--max-turns", "1"],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        health.error_type = "transient_timeout"
        health.detail = f"probe exceeded {timeout:.0f}s"
        return health
    output = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 0 and output:
        health.authenticated = True
        health.model_available = True
        health.inference_ok = True
        health.healthy = True
        health.detail = output[:120]
        return health
    status = _status_from_text(err + " " + output)
    health.error_type = classify_executor_error(f"{err} {output}", status)
    health.detail = (err or output)[:300]
    health.authenticated = health.error_type != "auth_failed"
    return health


def _probe_grok(executor, timeout: float) -> ExecutorHealth:
    health = ExecutorHealth(id=executor.id, label=executor.label, kind=executor.kind)
    installed, detail = executor.available()
    health.installed = installed
    health.detail = detail
    if not installed:
        health.error_type = "not_installed"
        return health
    health.version = executor.version()
    try:
        result = _run_bounded(
            ["grok", "-p", "Reply with exactly: OK"], timeout=timeout
        )
    except subprocess.TimeoutExpired:
        health.error_type = "transient_timeout"
        health.detail = f"probe exceeded {timeout:.0f}s"
        return health
    output = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 0 and output:
        health.authenticated = True
        health.model_available = True
        health.inference_ok = True
        health.healthy = True
        health.detail = output[:120]
        return health
    status = _status_from_text(err + " " + output)
    health.error_type = classify_executor_error(f"{err} {output}", status)
    health.detail = (err or output)[:300]
    return health


def _probe_codex(executor, timeout: float) -> ExecutorHealth:
    health = ExecutorHealth(id=executor.id, label=executor.label, kind=executor.kind)
    installed, detail = executor.available()
    health.installed = installed
    health.detail = detail
    if not installed:
        health.error_type = "not_installed"
        return health
    health.version = executor.version()
    try:
        result = _run_bounded(
            ["codex", "exec", "--json", "--skip-git-repo-check", "-s", "read-only",
             "Reply with exactly: OK"],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        health.error_type = "transient_timeout"
        health.detail = f"probe exceeded {timeout:.0f}s"
        return health
    output = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 0 and output:
        health.authenticated = True
        health.model_available = True
        health.inference_ok = True
        health.healthy = True
        health.detail = output[:120]
        return health
    status = _status_from_text(err + " " + output)
    health.error_type = classify_executor_error(f"{err} {output}", status)
    health.detail = (err or output)[:300]
    return health


def _status_from_text(text: str) -> Optional[int]:
    match = re.search(r"\b(40[0-9]|41[0-9]|42[0-9]|429|5[0-9]{2})\b", text or "")
    return int(match.group(1)) if match else None


def _parse_ts(iso: str) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _iso_in(seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
