"""
Stage 8 test isolation.

The production database path is configurable via YODAW_DB_PATH.
Tests set it to a session-unique temp file before app modules are
imported, so:

- tests never touch data/yodaw.db (the developer's real DB)
- single-flight duplicate detection can't 409 across test runs
- each pytest session starts with a clean mission/learning store
"""

import os
import sys
import tempfile
import pytest
from pathlib import Path

_TMPDIR = tempfile.mkdtemp(prefix="yodaw_test_")
os.environ["YODAW_DB_PATH"] = str(Path(_TMPDIR) / "test_yodaw.db")

# Session-unique import name for tests.helpers so pytest does not
# collide with other packages named 'helpers' under --import-mode
# importlib, while keeping 'from tests.helpers import ...' working.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Make project-root imports work regardless of invocation dir.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True, scope="module")
def _clean_admin_table():
    """
    Reset every credential surface before and after each module.

    Authentication resolves against live store state (see
    app/api/auth.py), so an identity created by one module must
    never leak into the next. A leaked admin or client would
    silently flip the following module out of local-open
    development mode, making its auth behaviour depend on test
    ordering instead of on what the module itself configured.
    """
    from app.tenants.admins import AdminStore
    from app.tenants.clients import ClientStore
    from pathlib import Path

    db_path = Path(os.environ["YODAW_DB_PATH"])

    # Constructing the stores creates the tables when missing.
    AdminStore(db_path)
    ClientStore(db_path)

    import sqlite3

    def _reset() -> None:
        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM api_admins")
            conn.execute("DELETE FROM api_clients")
            conn.commit()

    _reset()
    yield
    _reset()
