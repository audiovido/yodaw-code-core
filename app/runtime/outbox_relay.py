"""
Stage 10.6: generalized exactly-once outbox.

Message kinds are typed payloads with registered handlers. The
relay drains the durable outbox and dispatches each message to
its handler; semantics:

- enqueue is transactional with the producer's work; a crash
  between commit and delivery loses nothing
- delivery is at-least-once at the transport level; handlers that
  destinations make idempotent (deterministic keys / upserts)
  produce exactly-once observable effects. This is NOT a claim of
  universal exactly-once delivery to arbitrary external systems:
  the outbox guarantees at-least-once with deduplication hooks.
- failures retry with exponential backoff (schedule owned by the
  store, identical for every backend) and dead-letter after
  OUTBOX_MAX_ATTEMPTS
- unknown kinds are never dropped: the handler failure marks the
  message failed (retry -> dead-letter), preserving it for a
  future relay version or operator requeue
- restart-safe: pending messages are picked up by the next relay
  that starts, including messages left by a dead process

Payload schemas are validated before dispatch (pydantic), so a
malformed message dead-letters with a precise error instead of
corrupting a handler.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Callable

from pydantic import BaseModel, Field

from app.learning.engine import build_learning_record
from app.learning.store import LearningStore
from app.storage.sqlite_store import (
    MissionStore,
    OUTBOX_MAX_ATTEMPTS,
    OUTBOX_BACKOFF_BASE_SECONDS,
    OUTBOX_BACKOFF_MAX_SECONDS,
)

logger = logging.getLogger("yodaw.outbox")


# ---------------------------------------------------------
# Typed payload schemas (10.6)
# ---------------------------------------------------------


class LearningRecordPayload(BaseModel):
    mission_id: str | None = None
    goal: str
    worker: str | None = None
    success: bool
    evidence: list[dict] = Field(default_factory=list)
    result: dict = Field(default_factory=dict)
    record_id: str | None = None


class AuditExportRequestedPayload(BaseModel):
    requested_by: str
    mission_id: str | None = None
    client_id: str | None = None
    reason: str | None = None
    export_id: str | None = None


class WebhookDeliveryPayload(BaseModel):
    url: str
    event: str
    mission_id: str | None = None
    body: dict = Field(default_factory=dict)


PAYLOAD_SCHEMAS: dict[str, type[BaseModel]] = {
    "learning.record": LearningRecordPayload,
    "audit.export.requested": AuditExportRequestedPayload,
    "webhook.delivery": WebhookDeliveryPayload,
}


def validate_payload(kind: str, payload: dict) -> dict:
    """
    Validate one outbox payload against its typed schema.

    Returns the normalized payload dict. Raises ValueError for an
    unknown kind (callers decide policy; the relay's policy is to
    let the message fail into retry/dead-letter, never drop).
    """
    schema = PAYLOAD_SCHEMAS.get(kind)

    if schema is None:
        raise ValueError(f"unknown outbox kind: {kind}")

    return schema.model_validate(payload).model_dump()


Handler = Callable[[dict], None]

_HANDLERS: dict[str, Handler] = {}


def handler_for(kind: str):
    """Register a delivery handler for an outbox message kind."""

    def register(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return register


def _learning_sink_for(store) -> object:
    """
    Bind the learning sink to the same database as the outbox
    store, so delivery cannot drift across databases. Postgres
    stores get the Postgres learning sink; SQLite stores share
    the mission store's path.
    """
    from app.storage.pg_store import PostgresMissionStore, PostgresLearningStore

    if isinstance(store, PostgresMissionStore):
        return PostgresLearningStore(store.dsn)

    return LearningStore(store.path)


@handler_for("learning.record")
def _deliver_learning_record(payload: dict, *, sink=None) -> None:
    record = build_learning_record(
        mission_id=payload.get("mission_id"),
        goal=payload.get("goal", ""),
        worker=payload.get("worker"),
        success=bool(payload.get("success")),
        evidence=payload.get("evidence", []),
        result=payload.get("result", {}),
        record_id=payload.get("record_id"),
    )

    # Upsert by the deterministic id: replaying the same message
    # rewrites the identical row, so the observable effect stays
    # exactly-once.
    sink.save(record)


@handler_for("audit.export.requested")
def _deliver_audit_export(payload: dict) -> None:
    """
    Operational sink for audit export requests.

    The durable outbox guarantees the request is never lost; the
    concrete exporter attaches through the handler registry
    (handler_for("audit.export.requested")) in deployments that
    have one.
    """
    logger.info(
        "audit export requested by %s (export_id=%s, mission=%s)",
        payload.get("requested_by"),
        payload.get("export_id"),
        payload.get("mission_id"),
    )


@handler_for("webhook.delivery")
def _deliver_webhook(payload: dict) -> None:
    """
    Webhook/event delivery abstraction.

    The default handler logs only; real deployments register an
    HTTP-delivering handler via the registry. Delivery retries
    with backoff and dead-letters after the bounded attempts —
    the receiving URL must deduplicate by event id for
    exactly-once effect (at-least-once transport semantics).
    """
    logger.info(
        "webhook delivery pending to %s for event %s",
        payload.get("url"),
        payload.get("event"),
    )


# ---------------------------------------------------------
# Relay
# ---------------------------------------------------------


class OutboxRelay:
    def __init__(
        self,
        store: MissionStore,
        poll_seconds: float = 0.5,
        kinds: list[str] | None = None,
    ):
        self.store = store
        self.poll_seconds = poll_seconds
        self.kinds = kinds
        self.learning_sink = _learning_sink_for(store)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivered = 0
        self._failures = 0
        self._dead = 0

    def stats(self) -> dict:
        counts = self.store.outbox_stats()
        return {
            **counts,
            "relay_delivered": self._delivered,
            "relay_failures": self._failures,
            "relay_alive": bool(
                self._thread and self._thread.is_alive()
            ),
        }

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run,
            name="yodaw-outbox-relay",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()

        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception:
                self._failures += 1
                logger.error(
                    "outbox pass failed\n%s", traceback.format_exc()
                )

            self._stop.wait(self.poll_seconds)

    def drain_once(self) -> int:
        """
        Deliver every due message once.

        Returns the number of messages delivered in this pass.
        Messages due after their backoff window are retried on a
        later pass; dead-lettered messages are never retried until
        an operator requeues them.
        """
        messages = self.store.outbox_pending(kinds=self.kinds)

        delivered = 0

        for message in messages:
            try:
                # Handler first, then an atomic acknowledgment: two
                # relays may both run the handler (at-least-once
                # transport), but exactly one wins the mark and a
                # replay is absorbed by the handler's idempotency
                # (deterministic record id / upsert).
                self._handle(message)

                if self.store.outbox_mark_delivered_if_pending(
                    message["id"]
                ):
                    delivered += 1
                    self._delivered += 1
            except Exception as exc:
                self._failures += 1
                self.store.outbox_mark_failed(
                    message["id"], f"{type(exc).__name__}: {exc}"
                )

                fresh = self.store.outbox_message(message["id"])

                if fresh and fresh["dead_lettered_at"]:
                    self._dead += 1
                    logger.error(
                        "outbox message %s dead-lettered after %s "
                        "attempts: %s",
                        message["id"],
                        fresh["attempts"],
                        fresh["last_error"],
                    )
                else:
                    logger.error(
                        "outbox delivery failed for message %s\n%s",
                        message["id"],
                        traceback.format_exc(),
                    )

        return delivered

    def _handle(self, message: dict) -> None:
        kind = message["kind"]
        payload = message["payload"]

        # Typed schema validation before dispatch: malformed
        # messages fail loudly instead of corrupting handlers.
        normalized = validate_payload(kind, payload)

        registered = _HANDLERS.get(kind)

        if registered is None:
            # Unknown kinds are never dropped: they fail into the
            # retry/backoff path and eventually dead-letter with
            # their payload intact for inspection or requeue.
            raise ValueError(f"no handler registered for kind: {kind}")

        import inspect

        if "sink" in inspect.signature(registered).parameters:
            registered(normalized, sink=self.learning_sink)
        else:
            registered(normalized)
