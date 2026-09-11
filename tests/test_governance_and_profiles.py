"""
Stage 10.4 + 10.7: rate limiting, payload governance, and
deployment profile fail-fast validation.

Rate-limit tests use a fake clock (no sleeping); limiter state is
verified shared across store instances (multi-process safe).
"""

import json

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.api.governance import (
    GovernanceConfig,
    RateLimiter,
    check_payload_governance,
)
from app.config import ConfigError, load_config, profile_defaults
from app.storage.sqlite_store import MissionStore
from app.tenants.clients import ClientStore


# ---------------------------------------------------------
# Rate limiting (fake clock, hermetic)
# ---------------------------------------------------------


def test_rate_limiter_burst_and_refill_fake_clock():
    store = MissionStore(":memory:") if False else None
    import tempfile
    from pathlib import Path

    store = MissionStore(Path(tempfile.mkdtemp()) / "rl.db")
    clock = {"now": 1000.0}
    limiter = RateLimiter(
        store,
        clock=lambda: clock["now"],
        config=None,
        enabled=True,
    )
    limiter.config.requests_per_minute = 60  # 1 token/second refill
    limiter.config.burst = 3

    results = [limiter.check("c1")[0] for _ in range(3)]

    assert results == [True, True, True]
    assert limiter.check("c1")[0] is False

    allowed, retry_after = limiter.check("c1")

    assert allowed is False
    assert 0 < retry_after <= 60

    # Advance the fake clock 3 seconds: 3 tokens refill.
    clock["now"] += 3

    assert limiter.check("c1")[0] is True


def test_rate_limiter_state_shared_across_processes():
    import tempfile
    from pathlib import Path

    db = Path(tempfile.mkdtemp()) / "shared.db"
    a = RateLimiter(MissionStore(db), clock=lambda: 10.0, enabled=True)
    b = RateLimiter(MissionStore(db), clock=lambda: 10.0, enabled=True)

    a.config.burst = 1
    b.config.burst = 1

    assert a.check("client-x")[0] is True
    assert b.check("client-x")[0] is False, (
        "second process must observe the shared bucket"
    )


def test_rate_limiter_disabled_admits_everything():
    import tempfile
    from pathlib import Path

    store = MissionStore(Path(tempfile.mkdtemp()) / "off.db")
    limiter = RateLimiter(store, clock=lambda: 0.0, enabled=False)

    for _ in range(50):
        assert limiter.check("anyone")[0] is True


def test_rate_limit_rejection_is_audited(monkeypatch, tmp_path):
    # Run in the test session's isolated session DB; a unique
    # client name and unique goals keep this independent of other
    # tests ordering.
    monkeypatch.setenv("YODAW_PROFILE", "single-node")
    monkeypatch.setenv("YODAW_RATE_LIMIT_RPM", "1")
    monkeypatch.setenv("YODAW_RATE_LIMIT_BURST", "1")
    monkeypatch.delenv("YODAW_API_KEY", raising=False)

    client = TestClient(main_module.app)

    import uuid

    tenant = main_module.clients.create_client(
        f"rl-tenant-{uuid.uuid4().hex[:8]}"
    )

    headers = {"Authorization": f"Bearer {tenant['api_key']}"}

    codes = []
    for i in range(4):
        codes.append(
            client.post(
                "/api/v1/missions",
                json={
                    "goal": f"rate limited mission {uuid.uuid4().hex}",
                    "capability": "code",
                },
                headers=headers,
            ).status_code
        )

    assert 429 in codes

    rejected = main_module.audit.query(action="request.rate_limited")

    assert len(rejected) >= 1

    response = client.post(
        "/api/v1/missions",
        json={"goal": "rate limited mission", "capability": "code"},
        headers=headers,
    )

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert response.headers["X-RateLimit-Limit"] == "1"

    monkeypatch.delenv("YODAW_PROFILE", raising=False)
    monkeypatch.delenv("YODAW_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("YODAW_RATE_LIMIT_BURST", raising=False)


# ---------------------------------------------------------
# Payload governance
# ---------------------------------------------------------


def test_governance_goal_length_enforced():
    ok, reason = check_payload_governance(
        goal="x" * 10, metadata={}, config=GovernanceConfig(
            max_goal_length=100
        )
    )

    assert ok and reason == ""

    ok, reason = check_payload_governance(
        goal="x" * 101, metadata={}, config=GovernanceConfig(
            max_goal_length=100
        )
    )

    assert not ok
    assert "goal" in reason


def test_governance_metadata_size_enforced():
    ok, _ = check_payload_governance(
        goal="fine",
        metadata={"blob": "y" * 100},
        config=GovernanceConfig(max_metadata_bytes=10_000),
    )

    assert ok

    ok, reason = check_payload_governance(
        goal="fine",
        metadata={"blob": "y" * 200},
        config=GovernanceConfig(max_metadata_bytes=100),
    )

    assert not ok
    assert "metadata" in reason


def test_governance_rejects_unserializable_metadata():
    ok, reason = check_payload_governance(
        goal="fine",
        metadata={"bad": object()},
        config=GovernanceConfig(max_metadata_bytes=1000),
    )

    assert not ok
    assert "serializable" in reason


def test_api_governance_413_before_processing(monkeypatch):
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_PROFILE", raising=False)
    monkeypatch.setenv("YODAW_RATE_LIMIT_RPM", "0")

    client = TestClient(main_module.app)

    response = client.post(
        "/api/v1/missions",
        json={"goal": "z" * 5000, "capability": "code"},
    )

    assert response.status_code == 413

    monkeypatch.delenv("YODAW_RATE_LIMIT_RPM", raising=False)


# ---------------------------------------------------------
# Deployment profiles (10.7)
# ---------------------------------------------------------


def test_profile_local_allows_open_mode(monkeypatch):
    monkeypatch.delenv("YODAW_DATABASE_URL", raising=False)
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_HAS_IDENTITIES", raising=False)
    monkeypatch.setenv("YODAW_PROFILE", "local")

    cfg = load_config("local")

    assert cfg.profile == "local"
    assert cfg.require_auth is False
    assert cfg.backend == "sqlite"
    assert cfg.warnings == []


def test_profile_production_rejects_open_mode(monkeypatch):
    monkeypatch.setenv("YODAW_PROFILE", "production")
    monkeypatch.setenv("YODAW_DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_HAS_IDENTITIES", raising=False)

    with pytest.raises(ConfigError, match="requires authentication"):
        load_config("production")


def test_profile_production_rejects_sqlite(monkeypatch):
    monkeypatch.setenv("YODAW_PROFILE", "production")
    monkeypatch.delenv("YODAW_DATABASE_URL", raising=False)

    with pytest.raises(ConfigError, match="YODAW_DATABASE_URL"):
        load_config("production")


def test_profile_production_rejects_shared_key_only(monkeypatch):
    monkeypatch.setenv("YODAW_PROFILE", "production")
    monkeypatch.setenv("YODAW_DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("YODAW_API_KEY", "legacy-key")
    monkeypatch.delenv("YODAW_HAS_IDENTITIES", raising=False)
    monkeypatch.delenv("YODAW_SHARED_KEY_POLICY", raising=False)

    with pytest.raises(ConfigError, match="shared-key-only"):
        load_config("production")

    # Policy override downgrades to a loud warning.
    monkeypatch.setenv("YODAW_SHARED_KEY_POLICY", "warn")

    cfg = load_config("production")

    assert any("shared key" in w for w in cfg.warnings)


def test_profile_production_accepts_identities(monkeypatch):
    """Identities mode is derived from actual store state: create a
    real admin identity and production config accepts it."""
    import uuid

    import app.main as main_module

    monkeypatch.setenv("YODAW_PROFILE", "production")
    monkeypatch.setenv("YODAW_DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.delenv("YODAW_API_KEY", raising=False)

    main_module.admins.create_admin(
        f"prod-root-{uuid.uuid4().hex[:8]}", role="superadmin"
    )

    cfg = load_config("production")

    assert cfg.auth_mode == "identities"
    assert cfg.warnings == []


def test_multi_process_sqlite_multi_coordinator_rejected(monkeypatch):
    monkeypatch.setenv("YODAW_PROFILE", "multi-process")
    monkeypatch.delenv("YODAW_DATABASE_URL", raising=False)
    monkeypatch.setenv("YODAW_API_KEY", "k")
    monkeypatch.setenv("YODAW_COORDINATOR_COUNT", "2")
    monkeypatch.delenv("YODAW_ALLOW_SQLITE_MULTI_COORD", raising=False)

    with pytest.raises(ConfigError, match="multiple coordinators"):
        load_config("multi-process")

    # Explicit override survives with a warning.
    monkeypatch.setenv("YODAW_ALLOW_SQLITE_MULTI_COORD", "1")

    cfg = load_config("multi-process")

    assert any("SQLite" in w for w in cfg.warnings)


def test_multi_process_sqlite_single_coordinator_warns(monkeypatch):
    monkeypatch.setenv("YODAW_PROFILE", "multi-process")
    monkeypatch.delenv("YODAW_DATABASE_URL", raising=False)
    monkeypatch.setenv("YODAW_API_KEY", "k")
    monkeypatch.setenv("YODAW_COORDINATOR_COUNT", "1")

    cfg = load_config("multi-process")

    assert any("ceiling" in w for w in cfg.warnings)


def test_profile_defaults_shape():
    for profile in ("local", "single-node", "multi-process", "production"):
        defaults = profile_defaults(profile)

        for key in (
            "embed_coordinator",
            "backend",
            "require_auth",
            "watchdog",
            "rate_limits",
            "outbox_relay",
            "keep_debug_worktrees",
        ):
            assert key in defaults, (profile, key)


def test_unknown_profile_rejected(monkeypatch):
    with pytest.raises(ConfigError, match="unknown YODAW_PROFILE"):
        load_config("spaceship")


def test_runtime_status_exposes_safe_config(monkeypatch):
    import uuid

    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_PROFILE", raising=False)

    # An admin identity exists by this point in the module, so the
    # anonymous local-open path is closed by design; authenticate
    # explicitly rather than depending on request-path state.
    admin = main_module.admins.create_admin(
        f"status-root-{uuid.uuid4().hex[:8]}", role="superadmin"
    )
    headers = {"Authorization": f"Bearer {admin['api_key']}"}

    client = TestClient(main_module.app)

    response = client.get("/api/v1/runtime/status", headers=headers)

    assert response.status_code == 200

    body = response.json()

    assert body["configuration"]["profile"] in (
        "local", "single-node", "multi-process", "production", None
    )
    assert "defaults" in body["configuration"]
    # No secrets in runtime status.
    assert "YODAW_API_KEY" not in json.dumps(body)
    assert "api_key" not in json.dumps(body)
