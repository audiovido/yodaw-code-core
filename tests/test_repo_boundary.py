"""
Repository trust boundary and canonical repository identity.

Three protections all make a decision about one server-local
repository: admission authorization, single-flight deduplication, and
the per-repository lease. If they do not agree on one canonical
identity, the same directory can be reached twice under two
spellings and both protections silently stop working. These tests
prove they agree, and prove that the optional operator-set root list
cannot be escaped by an alias or a symlink.

Hermetic: temp directories, temp stores, no network, no providers.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.core.models import Mission
from app.main import app
from app.runtime.repo_identity import (
    RepoNotAllowed,
    authorized_repo_roots,
    canonical_repo_path,
    ensure_authorized,
    is_within_roots,
    repo_identity,
)
from app.storage.sqlite_store import DuplicateMission, MissionStore
from app.tenants.admins import AdminStore
from app.tenants.audit import AuditStore
from app.tenants.clients import ClientStore
from app.workers.repo_code_worker import RepoCodeWorker


@pytest.fixture()
def isolated_api(monkeypatch, tmp_path):
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_PROFILE", raising=False)
    monkeypatch.delenv("YODAW_REPO_ROOTS", raising=False)
    monkeypatch.setenv("YODAW_RATE_LIMIT_RPM", "0")
    monkeypatch.setenv("YODAW_EMBED_COORDINATOR", "0")

    db = tmp_path / "api.db"
    monkeypatch.setattr(main_module, "store", MissionStore(db))
    monkeypatch.setattr(main_module, "clients", ClientStore(db))
    monkeypatch.setattr(main_module, "admins", AdminStore(db))
    monkeypatch.setattr(main_module, "audit", AuditStore(db))

    from app.api.governance import RateLimiter
    from app.main import _rate_backend

    monkeypatch.setattr(
        main_module,
        "rate_limiter",
        RateLimiter(_rate_backend, enabled_provider=lambda: False),
    )

    yield main_module

    monkeypatch.delenv("YODAW_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("YODAW_EMBED_COORDINATOR", raising=False)


def _submit(client, **body):
    payload = {
        "goal": f"repo boundary {uuid.uuid4().hex}",
        "capability": "repo-code",
    }
    payload.update(body)
    return client.post("/api/v1/missions", json=payload)


# ---------------------------------------------------------
# Canonical identity
# ---------------------------------------------------------

def test_canonical_path_collapses_every_alias(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    canonical = canonical_repo_path(repo)

    assert canonical == os.path.realpath(str(repo))

    for spelling in (
        str(repo) + "/",
        str(repo) + "//",
        str(repo) + "/.",
        str(repo) + "/./",
        f"{tmp_path}/child/../repo",
        f"{tmp_path}//repo",
    ):
        assert canonical_repo_path(spelling) == canonical, spelling


def test_canonical_path_follows_symlinks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo)

    assert canonical_repo_path(alias) == canonical_repo_path(repo)


def test_canonical_path_handles_absent_and_blank_input(tmp_path):
    assert canonical_repo_path(None) is None
    assert canonical_repo_path("") is None
    assert canonical_repo_path("   ") is None

    # A path that does not exist still canonicalizes (lexically).
    absent = tmp_path / "not-there"

    assert canonical_repo_path(absent) == os.path.realpath(str(absent))


def test_repo_identity_matches_across_aliases_and_falls_back(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo)

    assert repo_identity(repo, "repo-code") == repo_identity(alias, "repo-code")
    assert repo_identity(None, "repo-code") == "capability:repo-code"
    assert repo_identity("", "code") == "capability:code"


def test_store_dedup_key_is_canonical(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo)

    def mission(repo_path):
        return Mission(
            goal="same goal",
            capability="repo-code",
            metadata={"repo_path": repo_path},
        )

    assert MissionStore._repo_key(mission(str(repo))) == MissionStore._repo_key(
        mission(str(alias))
    )
    assert MissionStore._repo_key(
        mission(str(repo) + "/")
    ) == MissionStore._repo_key(mission(str(repo)))


# ---------------------------------------------------------
# Alias cannot bypass single-flight or leases
# ---------------------------------------------------------

def test_single_flight_cannot_be_bypassed_by_a_path_alias(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo)

    store = MissionStore(tmp_path / "db.sqlite")

    store.enqueue(
        Mission(
            goal="identical goal",
            capability="repo-code",
            metadata={"repo_path": str(repo)},
        )
    )

    for spelling in (
        str(alias),
        str(repo) + "/",
        str(repo) + "/.",
        f"{tmp_path}/child/../repo",
    ):
        with pytest.raises(DuplicateMission):
            store.enqueue(
                Mission(
                    goal="identical goal",
                    capability="repo-code",
                    metadata={"repo_path": spelling},
                )
            )


def test_lease_key_is_identical_for_every_alias(tmp_path):
    """The coordinator keys leases from MissionStore._repo_key, so
    identical keys are what make same-repo exclusion work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo)

    keys = {
        MissionStore._repo_key(
            Mission(
                goal="lease",
                capability="repo-code",
                metadata={"repo_path": spelling},
            )
        )
        for spelling in (str(repo), str(alias), str(repo) + "/")
    }

    assert len(keys) == 1


# ---------------------------------------------------------
# Authorized repository roots
# ---------------------------------------------------------

def test_no_roots_configured_imposes_no_bound(monkeypatch, tmp_path):
    monkeypatch.delenv("YODAW_REPO_ROOTS", raising=False)

    assert authorized_repo_roots() == ()
    assert ensure_authorized(str(tmp_path / "anywhere")) is not None


def test_roots_allow_inside_and_deny_outside(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    # The root itself and anything beneath it are in scope, including
    # a target that does not exist yet.
    assert ensure_authorized(str(root)) is not None
    assert ensure_authorized(str(root / "project")) is not None

    with pytest.raises(RepoNotAllowed):
        ensure_authorized(str(outside))


def test_roots_are_not_prefix_matched(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    sibling = tmp_path / "allowed-sibling"
    sibling.mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    with pytest.raises(RepoNotAllowed):
        ensure_authorized(str(sibling))


def test_roots_deny_dotdot_escape(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    escape = str(root / ".." / "outside")

    with pytest.raises(RepoNotAllowed):
        ensure_authorized(escape)


def test_roots_deny_symlink_escape(monkeypatch, tmp_path):
    """The authorization decision is made on the resolved path, so a
    symlink inside an authorized root cannot point outside it."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    (root / "escape").symlink_to(outside)

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    with pytest.raises(RepoNotAllowed):
        ensure_authorized(str(root / "escape"))


def test_multiple_roots_are_pathsep_separated(monkeypatch, tmp_path):
    first = tmp_path / "one"
    first.mkdir()
    second = tmp_path / "two"
    second.mkdir()
    outside = tmp_path / "three"
    outside.mkdir()

    monkeypatch.setenv(
        "YODAW_REPO_ROOTS", os.pathsep.join([str(first), str(second)])
    )

    assert len(authorized_repo_roots()) == 2
    assert is_within_roots(str(first / "x"), authorized_repo_roots())

    with pytest.raises(RepoNotAllowed):
        ensure_authorized(str(outside))


# ---------------------------------------------------------
# HTTP admission boundary
# ---------------------------------------------------------

def test_api_refuses_repo_outside_authorized_roots(
    isolated_api, monkeypatch, tmp_path
):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    client = TestClient(app)

    denied = _submit(client, repo_path=str(outside))

    assert denied.status_code == 403
    assert "authorized roots" in denied.json()["detail"]

    rejected = [
        event
        for event in main_module.audit.query(limit=1000)
        if event["action"] == "mission.rejected"
        and event["data"].get("reason") == "repository_not_allowed"
    ]

    assert rejected, "a denied repository must leave an audit trail"


def test_api_refuses_symlink_escape(isolated_api, monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside)

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    client = TestClient(app)

    assert _submit(client, repo_path=str(root / "escape")).status_code == 403


def test_api_accepts_repo_inside_authorized_roots(
    isolated_api, monkeypatch, tmp_path
):
    root = tmp_path / "allowed"
    root.mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    client = TestClient(app)

    response = _submit(client, repo_path=str(root / "project"))

    assert response.status_code == 200


def test_api_rejects_conflicting_repository_references(isolated_api, tmp_path):
    first = tmp_path / "one"
    first.mkdir()
    second = tmp_path / "two"
    second.mkdir()

    client = TestClient(app)

    response = _submit(
        client,
        repo_path=str(first),
        metadata={"repo": str(second)},
    )

    assert response.status_code == 400
    assert "conflicting repository" in response.json()["detail"]


def test_api_accepts_agreeing_repository_references(isolated_api, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    client = TestClient(app)

    response = _submit(
        client,
        repo_path=str(repo),
        metadata={"repo": str(repo) + "/"},
    )

    assert response.status_code == 200


def test_metadata_alias_cannot_redirect_the_authorized_repo(
    isolated_api, tmp_path
):
    """The stored target must be the one authoritative canonical path,
    not a metadata spelling that survived into execution."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    other = tmp_path / "other"
    other.mkdir()

    client = TestClient(app)

    response = _submit(client, repo_path=str(allowed), metadata={"repo": str(allowed)})

    assert response.status_code == 200

    mission = main_module.store.get(response.json()["mission_id"])

    assert mission is not None
    assert mission.metadata["repo_path"] == canonical_repo_path(allowed)
    # The alias keys are gone, so nothing downstream can prefer them.
    assert "repo" not in mission.metadata
    assert "repo_ref" not in mission.metadata


def test_legacy_product_alias_shares_the_same_boundary(
    isolated_api, monkeypatch, tmp_path
):
    """The legacy product endpoints must not be a weaker side door."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    client = TestClient(app)

    for path in ("/api/v1/product/missions", "/api/v1/product-missions"):
        response = client.post(
            path,
            json={
                "goal": f"legacy alias {uuid.uuid4().hex}",
                "capability": "repo-code",
                "repo_path": str(outside),
            },
        )

        assert response.status_code == 403, path


def test_legacy_product_alias_still_reports_replay(
    isolated_api, tmp_path
):
    repo = tmp_path / "repo"
    repo.mkdir()

    key = f"alias-{uuid.uuid4().hex}"
    client = TestClient(app)

    first = client.post(
        "/api/v1/product-missions",
        json={
            "goal": f"alias replay {uuid.uuid4().hex}",
            "capability": "code",
            "repo_path": str(repo),
            "idempotency_key": key,
        },
    )

    assert first.status_code == 200
    assert first.json()["replayed"] is False

    second = client.post(
        "/api/v1/product-missions",
        json={
            "goal": f"alias replay {uuid.uuid4().hex}",
            "capability": "code",
            "idempotency_key": key,
        },
    )

    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["mission_id"] == first.json()["mission_id"]


# ---------------------------------------------------------
# Execution-time re-check
# ---------------------------------------------------------

def test_worker_refuses_unauthorized_repo_at_execution(
    monkeypatch, tmp_path
):
    """A mission admitted before the operator set the bound must not
    still reach the filesystem."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    result = RepoCodeWorker().execute(
        "do something", {"repo_path": str(outside)}
    )

    # WorkerResult is a dict subclass.
    assert result["success"] is False
    assert result["error"]["type"] == "RepoNotAllowed"


def test_worker_accepts_authorized_repo(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    repo = root / "project"
    repo.mkdir()
    (repo / ".git").mkdir()

    monkeypatch.setenv("YODAW_REPO_ROOTS", str(root))

    result = RepoCodeWorker().execute("noop", {"repo_path": str(repo)})

    # It gets past the boundary (and then fails for lack of a planner,
    # which is not what this test is about).
    assert result.get("error", {}).get("type") != "RepoNotAllowed"
