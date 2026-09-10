"""
Stage 10.6: generalized outbox.

Proves:
- typed payload schemas validate before dispatch
- handler registry dispatch (multiple kinds)
- duplicate replay (idempotency key and idempotent handler)
- crash before acknowledgement -> re-delivery, no duplicate effect
- handler failure -> retry with backoff, never dropped
- bounded attempts -> dead-letter
- unknown message kinds are never dropped
- restart-safe relay picks up orphaned messages
"""

import logging
import uuid

import pytest

from app.learning.engine import record_id_default
from app.learning.store import LearningStore
from app.runtime.outbox_relay import (
    OutboxRelay,
    PAYLOAD_SCHEMAS,
    validate_payload,
)
from app.storage.sqlite_store import (
    MissionStore,
    OUTBOX_MAX_ATTEMPTS,
)


def _enqueue(store, kind, payload, key=None):
    return store.outbox_enqueue(
        mission_id=payload.get("mission_id", "m_x"),
        kind=kind,
        payload=payload,
        idempotency_key=key,
    )


# ---------------------------------------------------------
# Typed schemas
# ---------------------------------------------------------


def test_payload_schemas_validate():
    good = validate_payload(
        "learning.record",
        {"goal": "g", "success": True, "mission_id": "m1"},
    )
    assert good["goal"] == "g"

    good = validate_payload(
        "audit.export.requested",
        {"requested_by": "ops-root"},
    )
    assert good["requested_by"] == "ops-root"

    good = validate_payload(
        "webhook.delivery",
        {"url": "https://example.test/hook", "event": "mission.completed"},
    )
    assert good["event"] == "mission.completed"


def test_payload_schema_rejects_malformed():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        validate_payload("learning.record", {"success": "not-a-bool", "goal": 5})

    with pytest.raises(ValidationError):
        validate_payload(
            "webhook.delivery", {"event": "missing-url"}
        )


def test_unknown_kind_fails_validation():
    with pytest.raises(ValueError, match="unknown outbox kind"):
        validate_payload("mystery.kind", {})


# ---------------------------------------------------------
# Handler registry + delivery
# ---------------------------------------------------------


def test_relay_delivers_multiple_kinds(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    relay = OutboxRelay(store=store)

    delivered = []

    from app.runtime import outbox_relay as relay_module

    relay_module._HANDLERS["test.ping"] = delivered.append

    from pydantic import BaseModel

    class TestPingPayload(BaseModel):
        n: int = 0

    relay_module.PAYLOAD_SCHEMAS["test.ping"] = TestPingPayload

    try:
        _enqueue(store, "test.ping", {"n": 1})
        _enqueue(
            store,
            "webhook.delivery",
            {"url": "https://x.test/h", "event": "e"},
        )

        assert relay.drain_once() == 2
        assert delivered == [{"n": 1}]
        assert store.outbox_stats()["delivered"] == 2
    finally:
        relay_module._HANDLERS.pop("test.ping", None)
        relay_module.PAYLOAD_SCHEMAS.pop("test.ping", None)


def test_unknown_kind_is_never_dropped(tmp_path, caplog):
    store = MissionStore(tmp_path / "db.sqlite")
    relay = OutboxRelay(store=store)

    message_id = _enqueue(store, "brand.new.kind", {"x": 1})

    with caplog.at_level(logging.ERROR, logger="yodaw.outbox"):
        assert relay.drain_once() == 0

    # Still pending (retry-scheduled), payload intact.
    stats = store.outbox_stats()

    assert stats["pending"] == 1

    message = store.outbox_message(message_id)

    assert message["attempts"] == 1
    assert message["payload"] == {"x": 1}
    assert "unknown outbox kind" in message["last_error"]


def test_unknown_kind_dead_letters_after_bounded_attempts(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    relay = OutboxRelay(store=store)

    message_id = _enqueue(store, "no.handler.kind", {"x": 1})

    for _ in range(OUTBOX_MAX_ATTEMPTS):
        # Force due immediately each pass (hermetic, no sleeping).
        import sqlite3

        db = sqlite3.connect(str(store.path))
        db.execute(
            "UPDATE mission_outbox SET next_attempt_at="
            "'2000-01-01T00:00:00+00:00'"
        )
        db.commit()
        db.close()

        relay.drain_once()

    message = store.outbox_message(message_id)

    assert message["dead_lettered_at"] is not None
    assert message["attempts"] == OUTBOX_MAX_ATTEMPTS
    assert store.outbox_stats()["dead_lettered"] == 1


def test_duplicate_enqueue_with_idempotency_key(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")

    first = _enqueue(
        store,
        "learning.record",
        {"goal": "g", "success": True, "mission_id": "m_idem"},
        key="idem-key-1",
    )
    second = _enqueue(
        store,
        "learning.record",
        {"goal": "g", "success": True, "mission_id": "m_idem"},
        key="idem-key-1",
    )

    assert first == second
    assert store.outbox_stats()["total"] == 1


def test_crash_before_ack_redelivers_without_duplicate_effect(tmp_path):
    db_path = tmp_path / "db.sqlite"
    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    payload = {
        "mission_id": "m_crash",
        "goal": "crash window mission",
        "worker": "w",
        "success": True,
        "evidence": [],
        "result": {},
        "record_id": record_id_default("m_crash"),
    }
    _enqueue(store, "learning.record", payload)

    relay = OutboxRelay(store=store)

    # Deliver twice without marking (simulates crash between
    # handler and ack): the handler is upsert-idempotent.
    messages = store.outbox_pending()
    relay._handle(messages[0])
    relay._handle(messages[0])

    store.outbox_mark_delivered(messages[0]["id"])

    records = [r for r in learning.list() if r.mission_id == "m_crash"]

    assert len(records) == 1, "replay must not duplicate the record"


def test_handler_failure_retries_with_backoff(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    relay = OutboxRelay(store=store)

    message_id = _enqueue(
        store,
        "learning.record",
        {
            "mission_id": "m_retry",
            "goal": "g",
            "success": True,
            "record_id": record_id_default("m_retry"),
        },
    )

    def failing_handler(payload, sink=None):
        raise RuntimeError("destination outage")

    from app.runtime import outbox_relay as relay_module

    original = relay_module._HANDLERS.get("learning.record")
    relay_module._HANDLERS["learning.record"] = failing_handler

    try:
        assert relay.drain_once() == 0

        message = store.outbox_message(message_id)

        assert message["attempts"] == 1
        assert "destination outage" in message["last_error"]
        assert message["next_attempt_at"] is not None
        assert message["dead_lettered_at"] is None
    finally:
        relay_module._HANDLERS["learning.record"] = original


def test_dead_letter_then_operator_requeue_delivers(tmp_path):
    db_path = tmp_path / "db.sqlite"
    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    payload = {
        "mission_id": "m_dl",
        "goal": "dead letter mission",
        "success": True,
        "record_id": record_id_default("m_dl"),
    }
    message_id = _enqueue(store, "learning.record", payload)

    relay = OutboxRelay(store=store)

    # Exhaust attempts with a failing handler.
    def failing(payload, sink=None):
        raise RuntimeError("persisting outage")

    from app.runtime import outbox_relay as relay_module

    original = relay_module._HANDLERS.get("learning.record")

    try:
        relay_module._HANDLERS["learning.record"] = failing

        for _ in range(OUTBOX_MAX_ATTEMPTS):
            import sqlite3

            db = sqlite3.connect(str(db_path))
            db.execute(
                "UPDATE mission_outbox SET next_attempt_at="
                "'2000-01-01T00:00:00+00:00'"
            )
            db.commit()
            db.close()

            relay.drain_once()
    finally:
        relay_module._HANDLERS["learning.record"] = original

    assert store.outbox_message(message_id)["dead_lettered_at"]

    # Operator inspects and requeues.
    dead = store.outbox_list_dead()

    assert len(dead) == 1
    assert dead[0]["kind"] == "learning.record"

    assert store.outbox_requeue_dead(message_id) == 1

    # Handler fixed -> the requeued message delivers.
    assert relay.drain_once() == 1

    records = [r for r in learning.list() if r.mission_id == "m_dl"]

    assert len(records) == 1


def test_relay_restart_delivers_orphaned_messages(tmp_path):
    db_path = tmp_path / "db.sqlite"
    store = MissionStore(db_path)
    learning = LearningStore(db_path)

    payload = {
        "mission_id": "m_orphan",
        "goal": "orphaned learning",
        "success": True,
        "record_id": record_id_default("m_orphan"),
    }
    _enqueue(store, "learning.record", payload)

    # "Crash": first relay never drains; a fresh relay starts.
    relay = OutboxRelay(store=store)
    assert relay.drain_once() == 1

    records = [r for r in learning.list() if r.mission_id == "m_orphan"]

    assert len(records) == 1


def test_relay_respects_kind_filter(tmp_path):
    store = MissionStore(tmp_path / "db.sqlite")
    relay = OutboxRelay(store=store, kinds=["webhook.delivery"])

    _enqueue(
        store,
        "webhook.delivery",
        {"url": "https://x.test/h", "event": "e"},
    )
    _enqueue(
        store,
        "learning.record",
        {"goal": "g", "success": True, "mission_id": "m_kf"},
    )

    # Only the webhook kind is drained by this relay instance.
    assert relay.drain_once() == 1
    assert store.outbox_stats()["pending"] == 1
    assert store.outbox_pending()[0]["kind"] == "learning.record"
