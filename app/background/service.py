"""Process-wide wiring for the background task subsystem.

One store, one event bus, one engine per process — the same posture the
existing runtime takes for the mission coordinator (API and engine in
one process, durable state in SQLite). Everything is lazily built so
importing the API module never opens a database, which keeps tests and
the CLI able to point ``KODGAR_TASKS_DB`` somewhere isolated first.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from app.background.engine import TaskEngine
from app.background.events import get_bus
from app.background.store import DEFAULT_TASK_DB, TaskStore

_lock = threading.Lock()
_store: Optional[TaskStore] = None
_engine: Optional[TaskEngine] = None


def task_db_path() -> str:
    return os.environ.get("KODGAR_TASKS_DB", str(DEFAULT_TASK_DB))


def get_store() -> TaskStore:
    global _store
    with _lock:
        if _store is None:
            _store = TaskStore(task_db_path())
        return _store


def get_engine() -> TaskEngine:
    """Build the engine once, without nesting the module lock.

    ``get_store()`` takes the same lock, so acquiring it here around a
    call to ``get_store()`` would deadlock (a plain Lock is not
    reentrant). The store is therefore resolved outside the critical
    section and the double-checked assignment happens inside it.
    """
    global _engine
    with _lock:
        engine = _engine
    if engine is not None:
        return engine

    store = get_store()
    with _lock:
        if _engine is None:
            _engine = TaskEngine(store=store, bus=get_bus(store))
        return _engine


def start_engine() -> TaskEngine:
    """Start workers + recovery; safe to call twice."""
    engine = get_engine()
    engine.start()
    return engine


def stop_engine(timeout: float = 20.0) -> None:
    global _engine
    with _lock:
        engine = _engine
    if engine is not None:
        engine.stop(timeout=timeout)


def reset_for_tests() -> None:
    """Drop process-wide singletons (tests only)."""
    global _store, _engine
    with _lock:
        if _engine is not None:
            try:
                _engine.stop(timeout=5)
            except Exception:
                pass
        _engine = None
        if _store is not None:
            _store.close()
        _store = None
