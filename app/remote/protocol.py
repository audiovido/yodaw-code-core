"""Remote worker protocol: versioned handshake and job messages.

Transport-agnostic contract. A future TCP/HTTP/WebSocket transport
can serialize these dataclasses without changing pool semantics.
No sockets or cloud dependencies live here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = 1
MIN_PROTOCOL_VERSION = 1


class ProtocolMismatchError(ValueError):
    """Worker protocol version is outside the supported range."""


class AuthRejectedError(PermissionError):
    """Worker credentials were rejected."""


class DuplicateIdentityError(ValueError):
    """A live worker already holds this identity."""


def negotiate_protocol(client_version: int) -> int:
    """Agree on a protocol version or raise ProtocolMismatchError."""
    if not isinstance(client_version, int) or isinstance(client_version, bool):
        raise ProtocolMismatchError(
            f"protocol version must be int, got {type(client_version).__name__}"
        )
    if client_version < MIN_PROTOCOL_VERSION or client_version > PROTOCOL_VERSION:
        raise ProtocolMismatchError(
            f"unsupported protocol version {client_version}; "
            f"supported [{MIN_PROTOCOL_VERSION}, {PROTOCOL_VERSION}]"
        )
    return min(client_version, PROTOCOL_VERSION)


@dataclass(frozen=True)
class RegisterRequest:
    worker_id: str
    capabilities: frozenset[str]
    capacity: int
    protocol_version: int = PROTOCOL_VERSION
    auth_token: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RegisterResponse:
    accepted: bool
    worker_id: str
    negotiated_version: int = PROTOCOL_VERSION
    reason: str = ""


@dataclass(frozen=True)
class HeartbeatMessage:
    worker_id: str
    used_slots: int = 0
    healthy: bool = True


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    goal: str
    capability: str
    metadata: dict[str, Any] = field(default_factory=dict)
    repo_key: str = ""


@dataclass(frozen=True)
class JobAssignment:
    job_id: str
    worker_id: str
    spec: JobSpec


@dataclass(frozen=True)
class JobResult:
    job_id: str
    worker_id: str
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None


@dataclass(frozen=True)
class CancelRequest:
    job_id: str
    reason: str = "cancelled"
