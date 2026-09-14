"""
Shared SQLite connection hardening for the Stage 8 local runtime.

All stores share one connection policy:

- WAL journal mode for concurrent readers + one writer
- busy timeout so writers queue instead of erroring
- explicit transaction boundaries at the store layer

Store contracts are intentionally small and interface-shaped so a
future Postgres backend can replace SQLite without touching the
coordinator, workers, or API.

Fresh-install / corruption repair lives here too
(:func:`check_database`, :func:`repair_database`): a clean install,
a half-written file, or a stale WAL must never wedge the runtime
with an opaque ``unable to open database file`` again.
"""

import os
import sqlite3
import time
from pathlib import Path
from typing import Union

DB_PATH = Path(os.environ.get("YODAW_DB_PATH", "data/yodaw.db"))

BUSY_TIMEOUT_MS = 30000


class ClosingConnection(sqlite3.Connection):
    """sqlite3 connection whose ``with`` exit actually closes it.

    The stdlib connection context manager only commits/rolls back; it
    leaves the handle open until garbage collection, which lets FDs
    pile up linearly under sustained multi-threaded load. Closing on
    exit keeps handle count bounded by in-flight operations.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def connect(path: Union[Path, str] = DB_PATH) -> sqlite3.Connection:
    """Open a hardened SQLite connection."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise sqlite3.OperationalError(
            f"cannot create database directory {path.parent}: {exc}. "
            f"Set YODAW_DB_PATH to a writable location."
        ) from exc

    try:
        db = sqlite3.connect(
            path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            factory=ClosingConnection,
        )
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError as exc:
        raise sqlite3.OperationalError(
            f"cannot open SQLite database {path}: {exc}. "
            f"Run `python -m app.operations db-repair --db {path}` "
            f"to diagnose and repair (corrupt files are backed up, "
            f"never deleted)."
        ) from exc

    return db


def check_database(path: Union[Path, str] = DB_PATH) -> dict:
    """Read-only health check. Never raises.

    Returns ``{"ok": True, ...}`` for a healthy database (existing
    or cleanly creatable) and ``{"ok": False, "error": ...}`` with
    an actionable message otherwise.
    """
    path = Path(path)
    report: dict = {"path": str(path), "exists": path.exists()}

    parent = path.parent
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
            report["created_parent"] = True
        except OSError as exc:
            report["ok"] = False
            report["error"] = (
                f"database directory {parent} is not creatable: {exc}. "
                f"Set YODAW_DB_PATH to a writable location."
            )
            return report
    if not os.access(parent, os.W_OK):
        report["ok"] = False
        report["error"] = (
            f"database directory {parent} is not writable. "
            f"Set YODAW_DB_PATH to a writable location."
        )
        return report

    if path.exists() and not os.access(path, os.W_OK):
        report["ok"] = False
        report["error"] = (
            f"database file {path} is not writable. Fix permissions "
            f"or set YODAW_DB_PATH to a writable location."
        )
        return report

    try:
        db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
        try:
            db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            integrity = db.execute("PRAGMA integrity_check").fetchone()
            report["integrity_check"] = (
                integrity[0] if integrity else "unknown"
            )
            if report["integrity_check"] != "ok":
                report["ok"] = False
                report["error"] = (
                    f"database {path} failed integrity_check: "
                    f"{report['integrity_check']}. Run "
                    f"`python -m app.operations db-repair --db {path}`."
                )
                return report
            try:
                report["user_version"] = db.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
            except sqlite3.DatabaseError:
                report["user_version"] = None
            tables = [
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            report["tables"] = tables
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        report["ok"] = False
        report["error"] = (
            f"database {path} is not a readable SQLite file: {exc}. "
            f"Run `python -m app.operations db-repair --db {path}` "
            f"(the corrupt file is backed up, never deleted)."
        )
        return report
    except sqlite3.OperationalError as exc:
        report["ok"] = False
        report["error"] = f"cannot open database {path}: {exc}."
        return report

    report["ok"] = True
    return report


def _backup_corrupt(path: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    backup = path.with_name(f"{path.name}.corrupt-{stamp}.bak")
    counter = 0
    while backup.exists():
        counter += 1
        backup = path.with_name(
            f"{path.name}.corrupt-{stamp}-{counter}.bak"
        )
    # Move the main file plus any WAL/SHM sidecars together.
    path.rename(backup)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            try:
                sidecar.rename(Path(str(backup) + suffix))
            except OSError:
                pass
    return backup


def repair_database(
    path: Union[Path, str] = DB_PATH,
    *,
    backup: bool = True,
) -> dict:
    """Diagnose and repair the SQLite database. Never raises.

    Strategy: missing file -> create schema fresh; healthy file ->
    no-op; corrupt/unreadable file -> move aside to
    ``<name>.corrupt-<utc>.bak`` (+ WAL/SHM sidecars) and recreate
    the schema fresh. Returns a report dict with ``ok``,
    ``repaired``, ``actions``, ``backup`` and ``error`` keys.
    """
    from app.storage.sqlite_store import MissionStore

    path = Path(path)
    actions: list[str] = []

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "path": str(path),
            "ok": False,
            "repaired": False,
            "actions": actions,
            "backup": None,
            "error": (
                f"database directory {path.parent} is not creatable: "
                f"{exc}. Set YODAW_DB_PATH to a writable location."
            ),
        }

    if not path.exists():
        actions.append("database file missing; creating fresh schema")
        try:
            MissionStore(path)
        except Exception as exc:
            return {
                "path": str(path),
                "ok": False,
                "repaired": False,
                "actions": actions,
                "backup": None,
                "error": f"fresh schema creation failed: {exc}",
            }
        actions.append("fresh schema created")
        return {
            "path": str(path),
            "ok": True,
            "repaired": True,
            "actions": actions,
            "backup": None,
            "error": None,
        }

    status = check_database(path)
    if status.get("ok"):
        return {
            "path": str(path),
            "ok": True,
            "repaired": False,
            "actions": ["database healthy; no repair needed"],
            "backup": None,
            "error": None,
        }

    # Permission problems are not repairable by us.
    error = str(status.get("error", ""))
    if "not writable" in error:
        return {
            "path": str(path),
            "ok": False,
            "repaired": False,
            "actions": actions,
            "backup": None,
            "error": error,
        }

    actions.append(f"diagnosed: {error}")
    backup_path = None
    if backup:
        try:
            backup_path = _backup_corrupt(path)
        except OSError as exc:
            return {
                "path": str(path),
                "ok": False,
                "repaired": False,
                "actions": actions,
                "backup": None,
                "error": (
                    f"cannot move corrupt database aside: {exc}. Fix "
                    f"permissions on {path.parent} and retry."
                ),
            }
        actions.append(f"corrupt file backed up to {backup_path}")
    else:
        try:
            path.unlink()
        except OSError as exc:
            return {
                "path": str(path),
                "ok": False,
                "repaired": False,
                "actions": actions,
                "backup": None,
                "error": f"cannot remove corrupt database: {exc}",
            }
        actions.append("corrupt file removed (backup disabled)")

    try:
        MissionStore(path)
    except Exception as exc:
        return {
            "path": str(path),
            "ok": False,
            "repaired": False,
            "actions": actions,
            "backup": str(backup_path) if backup_path else None,
            "error": f"schema recreation failed after backup: {exc}",
        }
    actions.append("fresh schema recreated")

    final = check_database(path)
    if not final.get("ok"):
        return {
            "path": str(path),
            "ok": False,
            "repaired": True,
            "actions": actions,
            "backup": str(backup_path) if backup_path else None,
            "error": f"recreated database still unhealthy: {final.get('error')}",
        }
    return {
        "path": str(path),
        "ok": True,
        "repaired": True,
        "actions": actions,
        "backup": str(backup_path) if backup_path else None,
        "error": None,
    }
