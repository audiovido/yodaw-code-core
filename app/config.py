"""
Stage 10.7: deployment profiles and fail-fast configuration.

Profiles:

- local:         embedded coordinator, SQLite, no auth; the
                 Stage 7/8/9 default for a developer laptop
- single-node:   embedded coordinator, SQLite, auth required
- multi-process: split API + coordinator processes over one
                 database; auth required; Postgres strongly
                 recommended (SQLite only with a loud warning
                 and a documented ceiling)
- production:    split or embedded, Postgres required, auth
                 required, no shared-key-only config, open mode
                 refused outright

Validation fails fast at startup on unsafe combinations:

- production + no auth (open local-dev mode)     -> ConfigError
- production + shared key only (no admin identities) -> error
  (hard error by default; set YODAW_SHARED_KEY_POLICY=warn to
  downgrade to a loud warning)
- production + SQLite + multiple coordinators    -> ConfigError
- production + SQLite at all                     -> ConfigError
  (Postgres is the production database; multi-process may use
  SQLite on a single node only)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Unsafe configuration; the process must refuse to start."""


PROFILES = ("local", "single-node", "multi-process", "production")


@dataclass
class RuntimeConfig:
    """Validated runtime configuration snapshot."""

    profile: str
    embed_coordinator: bool
    backend: str                    # sqlite | postgres
    database_url: str | None        # Postgres DSN, if backend=postgres
    db_path: str                    # SQLite path, if backend=sqlite
    auth_mode: str                  # open | shared-key | identities
    require_auth: bool
    max_concurrent_missions: int
    heartbeat_seconds: int
    stale_after_seconds: int
    embed_relay: bool = True
    log_level: str = "INFO"
    keep_debug_worktrees: bool = field(default=False)
    warnings: list[str] = field(default_factory=list)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _auth_mode() -> str:
    """
    Which authentication surface is configured.

    - identities: admin identities exist in the store (RBAC
      surface) — derived from actual store state, accurate in the
      serving process and stable across restarts
    - shared-key: only YODAW_API_KEY is set (legacy surface)
    - open:       nothing is configured (local-dev open mode)
    """
    try:
        from app.tenants.admins import AdminStore
        from app.storage.db import DB_PATH

        if AdminStore(DB_PATH).count_admins() > 0:
            return "identities"
    except Exception:
        pass

    if os.environ.get("YODAW_API_KEY"):
        return "shared-key"

    return "open"


def load_config(profile: str | None = None) -> RuntimeConfig:
    """
    Read the environment into a RuntimeConfig and validate it.

    Raises ConfigError on unsafe combinations. Warnings are
    attached for loud-but-survivable conditions.
    """
    profile = (profile or os.environ.get("YODAW_PROFILE", "local")).strip()
    auth_mode = _auth_mode()

    database_url = os.environ.get("YODAW_DATABASE_URL") or None
    backend = "postgres" if database_url else "sqlite"
    embed = os.environ.get("YODAW_EMBED_COORDINATOR", "1") == "1"
    coordinators = _env_int("YODAW_COORDINATOR_COUNT", 1 if embed else 2)
    shared_key_policy = os.environ.get(
        "YODAW_SHARED_KEY_POLICY", "error"
    ).strip().lower()

    warnings: list[str] = []

    if profile not in PROFILES:
        raise ConfigError(
            f"unknown YODAW_PROFILE {profile!r}; expected one of "
            f"{PROFILES}"
        )

    require_auth = profile in ("single-node", "multi-process", "production")

    # -------------------------------------------------
    # Production hard stops
    # -------------------------------------------------
    if profile == "production":
        if backend != "postgres":
            raise ConfigError(
                "production requires YODAW_DATABASE_URL (Postgres); "
                "SQLite is not a production database backend"
            )

        if auth_mode == "open":
            raise ConfigError(
                "production requires authentication: configure admin "
                "identities (YODAW_HAS_IDENTITIES=1) or a shared key"
            )

        if auth_mode == "shared-key":
            if shared_key_policy == "warn":
                warnings.append(
                    "production is running with the legacy shared key "
                    "as the only admin surface; migrate to admin "
                    "identities (YODAW_SHARED_KEY_POLICY=warn silences "
                    "this from being fatal)"
                )
            else:
                raise ConfigError(
                    "production refuses shared-key-only configuration; "
                    "create admin identities or set "
                    "YODAW_SHARED_KEY_POLICY=warn to override"
                )

    # -------------------------------------------------
    # Multi-process + SQLite
    # -------------------------------------------------
    if profile == "multi-process" and backend == "sqlite":
        warnings.append(
            "multi-process profile on SQLite: practical ceiling is a "
            "single node with low write concurrency (busy-timeout "
            "serialization); use Postgres for scale-out"
        )

    if (
        profile in ("multi-process", "production")
        and backend == "sqlite"
        and coordinators > 1
        and os.environ.get("YODAW_ALLOW_SQLITE_MULTI_COORD") != "1"
    ):
        raise ConfigError(
            "multiple coordinators on SQLite refuse to start; use "
            "Postgres or set YODAW_ALLOW_SQLITE_MULTI_COORD=1 to "
            "accept the single-node ceiling explicitly"
        )

    if require_auth and auth_mode == "open":
        raise ConfigError(
            f"profile {profile!r} requires authentication but no key "
            "surface is configured"
        )

    return RuntimeConfig(
        profile=profile,
        embed_coordinator=embed,
        backend=backend,
        database_url=database_url,
        db_path=os.environ.get("YODAW_DB_PATH", "data/yodaw.db"),
        auth_mode=auth_mode,
        require_auth=require_auth,
        max_concurrent_missions=_env_int(
            "YODAW_MAX_CONCURRENT_MISSIONS", 2
        ),
        heartbeat_seconds=_env_int("YODAW_HEARTBEAT_SECONDS", 30),
        stale_after_seconds=_env_int(
            "YODAW_STALE_AFTER_SECONDS",
            _env_int("YODAW_HEARTBEAT_SECONDS", 30) * 4,
        ),
        embed_relay=os.environ.get("YODAW_EMBED_RELAY", "1") == "1",
        log_level=os.environ.get("YODAW_LOG_LEVEL", "INFO"),
        keep_debug_worktrees=(
            os.environ.get("YODAW_KEEP_DEBUG_WORKTREES", "0") == "1"
        ),
        warnings=warnings,
    )


def profile_defaults(profile: str) -> dict:
    """Documented defaults per profile (for /runtime/status)."""
    defaults = {
        "local": {
            "embed_coordinator": True,
            "backend": "sqlite",
            "require_auth": False,
            "max_concurrent_missions": 2,
            "watchdog": True,
            "rate_limits": "off",
            "log_level": "INFO",
            "outbox_relay": "embedded",
            "keep_debug_worktrees": False,
        },
        "single-node": {
            "embed_coordinator": True,
            "backend": "sqlite",
            "require_auth": True,
            "max_concurrent_missions": 4,
            "watchdog": True,
            "rate_limits": "default",
            "log_level": "INFO",
            "outbox_relay": "embedded",
            "keep_debug_worktrees": False,
        },
        "multi-process": {
            "embed_coordinator": False,
            "backend": "postgres",
            "require_auth": True,
            "max_concurrent_missions": 8,
            "watchdog": True,
            "rate_limits": "default",
            "log_level": "INFO",
            "outbox_relay": "standalone",
            "keep_debug_worktrees": False,
        },
        "production": {
            "embed_coordinator": False,
            "backend": "postgres",
            "require_auth": True,
            "max_concurrent_missions": 8,
            "watchdog": True,
            "rate_limits": "default+per-client",
            "log_level": "WARNING",
            "outbox_relay": "standalone",
            "keep_debug_worktrees": False,
        },
    }

    return defaults.get(profile, defaults["local"])
