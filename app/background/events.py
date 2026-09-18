"""Live event fan-out for background tasks.

Durability first: every event is committed to the task store *before*
it is handed to any subscriber. Two consequences matter:

- a browser that reconnects replays from the store by ``seq`` and can
  never miss an event, even across a runtime restart
- an SSE consumer that is slow or dead costs memory in one bounded
  queue, never correctness

The bus is in-process (the runtime serves API + engine in one process,
exactly like the existing mission coordinator), and it exposes a
wildcard subscription so the task list screen can update live too.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Iterable, Optional

from app.background.models import TERMINAL_STATES, TaskEvent
from app.background.store import TaskStore

WILDCARD = "*"

# Event types that end a task's life. A reader that has seen one of
# these has seen everything there will ever be.
TERMINAL_EVENTS = frozenset(
    {"task.completed", "task.failed", "task.cancelled", "task.blocked"}
)

# Bounded so a dead consumer cannot grow the runtime without limit.
SUBSCRIBER_QUEUE_SIZE = 512


class TaskEventBus:
    """Store-backed publish/subscribe for task events."""

    def __init__(self, store: TaskStore):
        self.store = store
        self._lock = threading.Lock()
        self._subscribers: dict[str, set[queue.Queue]] = {}

    # -------------------------------------------------------- publish
    def publish(
        self, task_id: str, event_type: str, data: Optional[dict] = None
    ) -> TaskEvent:
        event = self.store.append_event(task_id, event_type, data or {})
        self._fan_out(event)
        return event

    def _fan_out(self, event: TaskEvent) -> None:
        with self._lock:
            targets = set(self._subscribers.get(event.task_id, ()))
            targets |= set(self._subscribers.get(WILDCARD, ()))
        for sink in targets:
            try:
                sink.put_nowait(event)
            except queue.Full:
                # A stalled consumer is dropped from the live feed; it
                # still recovers on reconnect because the store holds
                # the full ordered history.
                pass

    # ------------------------------------------------------ subscribe
    def subscribe(self, task_id: str) -> queue.Queue:
        sink: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        with self._lock:
            self._subscribers.setdefault(task_id, set()).add(sink)
        return sink

    def unsubscribe(self, task_id: str, sink: queue.Queue) -> None:
        with self._lock:
            bucket = self._subscribers.get(task_id)
            if bucket is not None:
                bucket.discard(sink)
                if not bucket:
                    self._subscribers.pop(task_id, None)

    def subscriber_count(self, task_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(task_id, ()))

    # ---------------------------------------------------- stream API
    def stream(
        self,
        task_id: str,
        after_seq: int = 0,
        poll_seconds: float = 15.0,
        stop: Optional[Callable[[], bool]] = None,
        stop_at_terminal: bool = True,
    ) -> Iterable[Optional[TaskEvent]]:
        """Yield stored events then live ones.

        ``None`` is yielded on poll timeouts so the caller can emit an
        SSE heartbeat/completion check; consumers must tolerate it.
        Replay covers the gap between the client's last seen ``seq`` and
        subscription, which is what makes reconnect exact.

        A terminal task ends the stream: the consumer gets every event
        (including the terminal one) and then the generator returns, so
        a finished task cannot leave a reader hanging forever.
        """
        sink = self.subscribe(task_id)
        try:
            cursor = int(after_seq)
            for event in self.store.events(task_id, after_seq=cursor):
                cursor = max(cursor, event.seq)
                yield event
                if stop_at_terminal and event.type in TERMINAL_EVENTS:
                    return
            while True:
                if stop is not None and stop():
                    return
                try:
                    event = sink.get(timeout=poll_seconds)
                except queue.Empty:
                    # Catch anything committed while we were draining a
                    # batch (another process, another thread).
                    pending = self.store.events(task_id, after_seq=cursor)
                    if pending:
                        for missed in pending:
                            cursor = max(cursor, missed.seq)
                            yield missed
                            if stop_at_terminal and missed.type in TERMINAL_EVENTS:
                                return
                        continue
                    if self._is_terminal(task_id):
                        # The task went terminal while we were blocked on
                        # the queue; drain the store one last time so an
                        # event committed between the last get() and the
                        # state flip cannot be missed, then close.
                        for missed in self.store.events(task_id, after_seq=cursor):
                            cursor = max(cursor, missed.seq)
                            yield missed
                            if stop_at_terminal and missed.type in TERMINAL_EVENTS:
                                return
                        return
                    yield None
                    continue
                if event.seq <= cursor:
                    continue
                cursor = event.seq
                yield event
                if stop_at_terminal and event.type in TERMINAL_EVENTS:
                    return
        finally:
            self.unsubscribe(task_id, sink)

    def _is_terminal(self, task_id: str) -> bool:
        task = self.store.get(task_id)
        if task is None:
            return True
        return task.state in TERMINAL_STATES

    def sse_frames(
        self, task_id: str, after_seq: int = 0, source: Optional[TaskStore] = None
    ) -> Iterable[str]:
        """SSE frames for one task, including heartbeats."""
        for event in self.stream(task_id, after_seq=after_seq):
            if event is None:
                yield ": keep-alive\n\n"
                continue
            yield _frame(event)
            if event.type in TERMINAL_EVENTS:
                return


def _frame(event: TaskEvent) -> str:
    import json

    payload = json.dumps(
        {
            "seq": event.seq,
            "task_id": event.task_id,
            "type": event.type,
            "ts": event.ts,
            "data": event.data,
        }
    )
    return f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n"


def format_event(event: TaskEvent) -> str:
    """Public helper (used by tests and the CLI stream client)."""
    return _frame(event)


# --------------------------------------------------------------- util
_bus_lock = threading.Lock()
_bus: Optional[TaskEventBus] = None


def get_bus(store: Optional[TaskStore] = None) -> TaskEventBus:
    """Process-wide bus bound to the process-wide task store."""
    global _bus
    with _bus_lock:
        if _bus is None or (store is not None and _bus.store is not store):
            _bus = TaskEventBus(store or TaskStore())
        return _bus


def reset_bus_for_tests() -> None:
    global _bus
    with _bus_lock:
        _bus = None


def emit(store: TaskStore, task_id: str, event_type: str, **data: Any) -> TaskEvent:
    """Convenience: publish through the process bus for a given store."""
    return get_bus(store).publish(task_id, event_type, data)
