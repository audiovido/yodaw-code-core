"""
Stage 10.1: capability-oriented storage boundary.

The runtime, coordinator, and API depend on these protocols, not
on SQLite internals. Each protocol is deliberately coarse-grained:
one capability area per protocol (missions/queue, leases, clients,
admin, audit, outbox, learning), not one interface per SQL
statement.

Backends:

- ``app.storage.sqlite_store`` — SQLite (default, local mode)
- ``app.storage.pg_store``     — PostgreSQL (production mode)

Both adapters implement the same protocols; the protocol module
stays dependency-free (no sqlite3, no psycopg) so it can be used
for typing from anywhere in the app.

Queue-claim semantics (both backends):

- claim is one atomic transaction; two coordinators can never
  receive the same mission
- candidates ordered by priority (1 = highest), then submission
  time, then insertion order
- per-client concurrency quotas are enforced inside the claim
  transaction (race-free)
- skipped repo keys exclude same-repo claims for this coordinator
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.core.models import Mission


@runtime_checkable
class MissionStoreProtocol(Protocol):
    """Missions, queue claiming, heartbeats, events, outbox."""

    # ------------------------------------------------- CRUD
    def save(self, mission: Mission) -> None: ...

    def get(self, mission_id: str) -> Mission | None: ...

    def list(self) -> list[Mission]: ...

    def status_counts(self) -> dict[str, int]: ...

    # ------------------------------------------------- queue
    def enqueue(self, mission: Mission) -> None:
        """Insert QUEUED; raise DuplicateMission on single-flight."""
        ...

    def claim_next(
        self,
        coordinator_id: str,
        skip_repo_keys: set[str] | None = None,
        client_limits: dict[str, int] | None = None,
    ) -> Mission | None: ...

    def heartbeat(self, mission_id: str, coordinator_id: str) -> bool: ...

    def request_cancel(self, mission_id: str) -> str:
        """One of cancelled / requested / terminal / unknown."""
        ...

    def stale_executing(self, stale_after_seconds: int) -> list[Mission]: ...

    # ------------------------------------------------- events
    def record_event(
        self,
        mission_id: str,
        event_type: str,
        attempt: int = 0,
        data: dict | None = None,
    ) -> int: ...

    def events(self, mission_id: str) -> list[dict]: ...

    # ------------------------------------------------- quota
    def executing_count_for_client(self, client_id: str) -> int: ...

    # ------------------------------------------------- outbox
    def outbox_enqueue(
        self,
        *,
        mission_id: str,
        kind: str,
        payload: dict,
        idempotency_key: str | None = None,
    ) -> int: ...

    def outbox_pending(
        self, limit: int = 100, kinds: list[str] | None = None
    ) -> list[dict]: ...

    def outbox_mark_delivered(self, outbox_id: int) -> None: ...

    def outbox_mark_failed(self, outbox_id: int, error: str) -> None: ...

    def outbox_dead_letter(self, outbox_id: int) -> None: ...

    def outbox_requeue_dead(self, outbox_id: int | None = None) -> int: ...

    def outbox_stats(self) -> dict: ...

    def outbox_message(self, outbox_id: int) -> dict | None: ...

    # ------------------------------------------------- idempotency
    def idempotency_lookup(
        self, tenant_scope: str, idempotency_key: str
    ) -> str | None: ...

    def submit_idempotent_mission(
        self,
        mission: Mission,
        *,
        tenant_scope: str,
        idempotency_key: str | None,
    ) -> tuple[Mission, bool]:
        """Atomic claim+insert; returns (mission, replayed)."""
        ...

    # ------------------------------------------------- finalization
    def save_owned(self, mission: Mission, owner: str) -> Mission:
        """Owner-fenced save; raises on stale owner."""
        ...

    def finalize_mission(
        self,
        mission_id: str,
        *,
        owner: str | None = None,
        status=None,
        result: dict | None = None,
        evidence: list | None = None,
        error_class: str | None = None,
        event_type: str = "mission.completed",
        event_data: dict | None = None,
        outbox_kind: str = "learning.record",
        outbox_payload: dict | None = None,
        outbox_idempotency_key: str | None = None,
        require_executing: bool = True,
    ) -> Mission:
        """Atomic terminal commit: mission + event + outbox."""
        ...


@runtime_checkable
class LeaseManagerProtocol(Protocol):
    """Per-repo exclusion leases, stale-safe, cross-process."""

    def acquire(
        self,
        repo_key: str,
        owner: str,
        mission_id: str | None = None,
        stale_after_seconds: int = 300,
    ) -> bool: ...

    def heartbeat(self, repo_key: str, owner: str) -> bool: ...

    def release(self, repo_key: str, owner: str) -> None: ...

    def release_all(self, owner: str) -> None: ...

    def held_by(self, owner: str) -> set[str]: ...

    def holder(self, repo_key: str) -> str | None: ...


@runtime_checkable
class ClientStoreProtocol(Protocol):
    """Client identities + quota/priority configuration."""

    def create_client(
        self,
        name: str,
        priority: int = 5,
        max_concurrent_missions: int | None = None,
    ) -> dict: ...

    def set_disabled(self, name: str, disabled: bool) -> bool: ...

    def set_priority(self, name: str, priority: int) -> bool: ...

    def list_clients(self) -> list[dict]: ...

    def authenticate(self, plaintext_key: str): ...

    def count_clients(self) -> int: ...


@runtime_checkable
class AdminStoreProtocol(Protocol):
    """Admin identities (RBAC), Stage 10.3."""

    def create_admin(
        self,
        name: str,
        role: str,
    ) -> dict: ...

    def authenticate(self, plaintext_key: str): ...

    def set_disabled(self, name: str, disabled: bool) -> bool: ...

    def set_role(self, name: str, role: str) -> bool: ...

    def list_admins(self) -> list[dict]: ...

    def rotate_key(self, name: str) -> dict | None: ...


@runtime_checkable
class AuditStoreProtocol(Protocol):
    """Append-only tamper-evident audit trail (Stage 9 + 10.5)."""

    def append(
        self,
        *,
        client_id: str | None = None,
        actor: str | None = None,
        action: str,
        mission_id: str | None = None,
        data: dict | None = None,
    ) -> dict: ...

    def query(
        self,
        client_id: str | None = None,
        mission_id: str | None = None,
        action: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]: ...

    def count(self) -> int: ...

    def verify(self) -> dict: ...

    def prune(self, *, keep_days: int, archive_path=None) -> dict: ...


@runtime_checkable
class LearningStoreProtocol(Protocol):
    """Learning records (upsert-idempotent by record id)."""

    def save(self, record) -> None: ...

    def list(self) -> list: ...
