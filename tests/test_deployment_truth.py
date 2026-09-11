"""
Deployment truth: the process must describe, and refuse to start on,
the backend it is actually running.

The configuration layer can ask for PostgreSQL (`YODAW_DATABASE_URL`
present), but the API process only ever instantiates the SQLite
store. Left unchecked that produced the worst possible failure mode:
a clean startup, a `/health` and `/runtime/status` that claim
Postgres, and every mission silently written to a local SQLite file.

These tests pin both halves of the fix: startup refuses the mismatch,
and readiness reports the *instantiated* backend rather than the
configured one.

Hermetic: temp database paths, embedded coordinator disabled, no
network, no provider calls, no live Postgres.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.config import load_config
from app.main import app, instantiated_backend
from app.storage.pg_store import PostgresMissionStore


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    """A single-node SQLite deployment, no ambient configuration."""
    monkeypatch.setenv("YODAW_DB_PATH", str(tmp_path / "yodaw.db"))
    monkeypatch.setenv("YODAW_EMBED_COORDINATOR", "0")
    monkeypatch.delenv("YODAW_DATABASE_URL", raising=False)
    monkeypatch.delenv("YODAW_PROFILE", raising=False)
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_SHARED_KEY_POLICY", raising=False)

    # These tests drive the real lifespan, and the lifespan's shutdown
    # path stops whatever coordinator the module global points at. An
    # earlier module in the same session may have started one, so hide
    # it for the duration: the embedded coordinator is disabled here
    # anyway, and no other test's coordinator is collateral damage.
    monkeypatch.setattr(main_module, "_coordinator", None)

    yield monkeypatch


def _dashboard():
    return {
        "YODAW_PROFILE": "single-node",
        "YODAW_API_KEY": "deployment-truth-key",
    }


# ---------------------------------------------------------
# Instantiated backend reporting
# ---------------------------------------------------------

def test_instantiated_backend_follows_the_live_store(clean_env):
    assert instantiated_backend() == "sqlite"

    original = main_module.store

    # A real PostgresMissionStore instance, built without connecting.
    clean_env.setattr(
        main_module, "store", object.__new__(PostgresMissionStore)
    )

    try:
        assert instantiated_backend() == "postgres"
    finally:
        clean_env.setattr(main_module, "store", original)


def test_health_reports_the_instantiated_backend(clean_env):
    for name, value in _dashboard().items():
        clean_env.setenv(name, value)

    with TestClient(app) as client:
        body = client.get("/api/v1/health").json()

    assert body["storage_backend"] == "sqlite"
    assert body["profile"] == "single-node"


def test_runtime_status_separates_configured_from_instantiated(clean_env):
    for name, value in _dashboard().items():
        clean_env.setenv(name, value)

    headers = {"Authorization": "Bearer deployment-truth-key"}

    with TestClient(app) as client:
        body = client.get("/api/v1/runtime/status", headers=headers).json()

    configuration = body["configuration"]

    assert configuration["profile"] == "single-node"
    assert configuration["backend"] == "sqlite"
    assert configuration["instantiated_backend"] == "sqlite"


# ---------------------------------------------------------
# Fail fast on a backend the process cannot serve
# ---------------------------------------------------------

@pytest.mark.parametrize("profile", ["multi-process", "production"])
def test_startup_refuses_when_config_demands_postgres(
    clean_env, profile
):
    """A configured-but-unwired Postgres backend must stop the
    process instead of silently serving SQLite."""
    clean_env.setenv("YODAW_PROFILE", profile)
    clean_env.setenv(
        "YODAW_DATABASE_URL", "postgresql://user:pw@localhost:5432/yodaw"
    )
    clean_env.setenv("YODAW_API_KEY", "deployment-truth-key")
    # production refuses shared-key-only configuration on its own; the
    # point here is the backend mismatch, so allow the shared key.
    clean_env.setenv("YODAW_SHARED_KEY_POLICY", "warn")

    # The configuration layer accepts this combination...
    assert load_config(profile).backend == "postgres"

    # ...and startup refuses it.
    with pytest.raises(Exception) as excinfo:
        with TestClient(app):
            pass

    message = str(excinfo.value)

    assert "postgres" in message
    assert "sqlite" in message
    assert "not wired" in message


def test_startup_refuses_the_mismatch_before_serving_requests(clean_env):
    clean_env.setenv("YODAW_PROFILE", "multi-process")
    clean_env.setenv(
        "YODAW_DATABASE_URL", "postgresql://user:pw@localhost:5432/yodaw"
    )
    clean_env.setenv("YODAW_API_KEY", "deployment-truth-key")

    with pytest.raises(Exception):
        with TestClient(app) as client:
            # Unreachable: the process must not get this far.
            client.get("/api/v1/health")


def test_supported_single_node_sqlite_deployment_starts(clean_env):
    for name, value in _dashboard().items():
        clean_env.setenv(name, value)

    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200


def test_startup_still_rejects_unsafe_profiles(clean_env):
    """The pre-existing profile validation must keep working."""
    clean_env.setenv("YODAW_PROFILE", "production")
    clean_env.delenv("YODAW_DATABASE_URL", raising=False)

    with pytest.raises(Exception) as excinfo:
        with TestClient(app):
            pass

    assert "YODAW_DATABASE_URL" in str(excinfo.value)
