"""Remote worker pool public surface."""

from __future__ import annotations

from app.remote.auth import (
    AllowAllAuthenticator,
    StaticTokenAuthenticator,
    TokenAuthenticator,
)
from app.remote.fake import FakeRemoteWorker
from app.remote.pool import Assignment, RemoteWorkerPool, WorkerRecord, WorkerState
from app.remote.protocol import (
    PROTOCOL_VERSION,
    AuthRejectedError,
    CancelRequest,
    DuplicateIdentityError,
    HeartbeatMessage,
    JobAssignment,
    JobResult,
    JobSpec,
    ProtocolMismatchError,
    RegisterRequest,
    RegisterResponse,
    negotiate_protocol,
)
from app.remote.transport import DispatcherTransport, InMemoryTransport

__all__ = [
    "AllowAllAuthenticator",
    "Assignment",
    "AuthRejectedError",
    "CancelRequest",
    "DispatcherTransport",
    "DuplicateIdentityError",
    "FakeRemoteWorker",
    "HeartbeatMessage",
    "InMemoryTransport",
    "JobAssignment",
    "JobResult",
    "JobSpec",
    "ProtocolMismatchError",
    "PROTOCOL_VERSION",
    "RegisterRequest",
    "RegisterResponse",
    "RemoteWorkerPool",
    "StaticTokenAuthenticator",
    "TokenAuthenticator",
    "WorkerRecord",
    "WorkerState",
    "negotiate_protocol",
]
