"""
Stage 9.5: exactly-once outbox relay.

The coordinator writes learning messages into the durable outbox
in the same store as the mission, then a relay drains it:

- enqueue happens with the mission finalization, so a crash
  between mission completion and learning delivery loses nothing
- delivery is at-least-once at the transport level, and the
  deterministic learning-record id (derived from the mission id)
  makes the receiver upsert-idempotent — so the observable effect
  is exactly-once
- failed deliveries are recorded with their error and retried on
  the next pass; nothing is silently dropped

The relay runs on its own thread with the same never-silent
discipline as the Stage 8 heartbeat loop: every failure is
counted, logged with a traceback, and the loop survives.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback

from app.learning.engine import build_learning_record
from app.learning.store import LearningStore
from app.storage.sqlite_store import MissionStore

logger = logging.getLogger("yodaw.outbox")


class OutboxRelay:
    def __init__(
        self,
        store: MissionStore,
        poll_seconds: float = 0.5,
    ):
        self.store = store
        # The relay delivers through its own learning store bound
        # to the same database as the mission store.
        self.learning_store = LearningStore(store.path)
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivered = 0
        self._failures = 0

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
        Deliver every pending message once.

        Returns the number of messages delivered in this pass.
        The handler is upsert-idempotent by design, so a crash
        between delivery and mark-delivered causes a safe re-play
        rather than a duplicate record.
        """
        messages = self.store.outbox_pending()

        delivered = 0

        for message in messages:
            try:
                self._handle(message)
                self.store.outbox_mark_delivered(message["id"])
                delivered += 1
                self._delivered += 1
            except Exception as exc:
                self._failures += 1
                self.store.outbox_mark_failed(
                    message["id"], f"{type(exc).__name__}: {exc}"
                )
                logger.error(
                    "outbox delivery failed for message %s\n%s",
                    message["id"],
                    traceback.format_exc(),
                )

        return delivered

    def _handle(self, message: dict) -> None:
        kind = message["kind"]

        if kind == "learning.record":
            payload = message["payload"]
            record = build_learning_record(
                mission_id=payload.get("mission_id"),
                goal=payload.get("goal", ""),
                worker=payload.get("worker"),
                success=bool(payload.get("success")),
                evidence=payload.get("evidence", []),
                result=payload.get("result", {}),
                record_id=payload.get("record_id"),
            )

            # Upsert by the deterministic id: replaying the same
            # message rewrites the identical row, so the observable
            # effect stays exactly-once.
            self.learning_store.save(record)
        else:
            # Unknown kinds are logged and left pending rather than
            # dropped; a future relay version can handle them.
            raise ValueError(f"unknown outbox kind: {kind}")
