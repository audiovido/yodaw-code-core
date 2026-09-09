"""
Shared SQLite connection hardening for the Stage 8 local runtime.

All stores share one connection policy:

- WAL journal mode for concurrent readers + one writer
- busy timeout so writers queue instead of erroring
- explicit transaction boundaries at the store layer

Store contracts are intentionally small and interface-shaped so a
future Postgres backend can replace SQLite without touching the
coordinator, workers, or API.
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get("YODAW_DB_PATH", "data/yodaw.db"))

BUSY_TIMEOUT_MS = 30000


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Open a hardened SQLite connection."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA synchronous=NORMAL")

    return db
