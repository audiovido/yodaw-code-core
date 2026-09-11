"""
Request body size enforcement.

The declared `Content-Length` is advisory. A caller can omit it
(chunked transfer) or understate it, so a bound that only reads the
header is not a bound at all — the server would still read the whole
payload into memory. These tests prove the limit is applied to the
bytes actually received, that the handler stops being fed the moment
the limit is crossed, and that an honest declaration is still refused
without reading a single byte.

The ASGI-level tests drive the middleware directly so the receive
channel can be shaped precisely; the HTTP-level tests prove the same
behaviour through the real application.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import BodyLimitMiddleware, app


def _run_middleware(chunks, headers, max_bytes):
    """Drive the middleware over fake ASGI messages.

    Returns (bytes the inner app was allowed to drain, sent messages).
    """
    pending = [
        {"type": "http.request", "body": chunk, "more_body": True}
        for chunk in chunks
    ]
    pending.append({"type": "http.request", "body": b"", "more_body": False})

    sent = []
    drained = {"bytes": 0, "messages": 0}

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def inner_app(scope, receive, send_inner):
        while True:
            message = await receive()

            if message["type"] != "http.request":
                break

            drained["bytes"] += len(message.get("body") or b"")
            drained["messages"] += 1

            if not message.get("more_body"):
                break

        await send_inner(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send_inner({"type": "http.response.body", "body": b"{}"})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/missions",
        "headers": headers,
    }

    middleware = BodyLimitMiddleware(inner_app, max_bytes=max_bytes)

    asyncio.run(middleware(scope, receive, send))

    return drained, sent


def _status(sent) -> int:
    starts = [m for m in sent if m["type"] == "http.response.start"]

    assert starts, "middleware produced no response"

    return starts[0]["status"]


# ---------------------------------------------------------
# Declared Content-Length
# ---------------------------------------------------------

def test_declared_oversize_is_rejected_without_reading_a_byte():
    drained, sent = _run_middleware(
        [b"x" * 10],
        [(b"content-length", b"999999")],
        max_bytes=1024,
    )

    assert _status(sent) == 413
    assert drained["bytes"] == 0
    assert drained["messages"] == 0


def test_declared_undersize_does_not_bypass_the_received_byte_count():
    """A lying Content-Length must not keep the bound from firing."""
    drained, sent = _run_middleware(
        [b"x" * 2048],
        [(b"content-length", b"10")],
        max_bytes=1024,
    )

    assert _status(sent) == 413


# ---------------------------------------------------------
# Actual received bytes
# ---------------------------------------------------------

def test_absent_content_length_is_bounded_by_received_bytes():
    """The chunked-transfer case: no declaration at all."""
    drained, sent = _run_middleware(
        [b"x" * 700, b"y" * 700],
        [],
        max_bytes=1024,
    )

    assert _status(sent) == 413
    # The handler is cut off at the chunk that crossed the limit; it
    # never drains the remainder of an unbounded payload.
    assert drained["bytes"] <= 1024


def test_unbounded_stream_is_cut_off_early():
    chunks = [b"z" * 512 for _ in range(200)]
    drained, sent = _run_middleware(chunks, [], max_bytes=1024)

    assert _status(sent) == 413
    assert drained["bytes"] <= 1024
    assert drained["bytes"] < sum(len(c) for c in chunks)


def test_body_exactly_at_the_limit_is_allowed():
    drained, sent = _run_middleware([b"x" * 1024], [], max_bytes=1024)

    assert _status(sent) == 200
    assert drained["bytes"] == 1024


def test_body_one_byte_over_the_limit_is_rejected():
    _, sent = _run_middleware([b"x" * 1025], [], max_bytes=1024)

    assert _status(sent) == 413


def test_rejected_request_does_not_leak_the_handler_response():
    _, sent = _run_middleware([b"x" * 4096], [], max_bytes=128)

    bodies = [m for m in sent if m["type"] == "http.response.body"]

    assert _status(sent) == 413
    assert len(bodies) == 1
    assert bodies[0]["body"] == BodyLimitMiddleware.TOO_LARGE_BODY


def test_non_http_scope_passes_through():
    sent = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(message):
        sent.append(message)

    async def inner_app(scope, receive, send_inner):
        await send_inner({"type": "lifespan.startup.complete"})

    middleware = BodyLimitMiddleware(inner_app, max_bytes=8)

    asyncio.run(
        middleware({"type": "lifespan"}, receive, send)
    )

    assert sent == [{"type": "lifespan.startup.complete"}]


# ---------------------------------------------------------
# Through the real application
# ---------------------------------------------------------

@pytest.fixture()
def isolated_api(monkeypatch, tmp_path):
    monkeypatch.delenv("YODAW_API_KEY", raising=False)
    monkeypatch.delenv("YODAW_PROFILE", raising=False)
    monkeypatch.delenv("YODAW_REPO_ROOTS", raising=False)
    monkeypatch.setenv("YODAW_RATE_LIMIT_RPM", "0")
    monkeypatch.setenv("YODAW_EMBED_COORDINATOR", "0")

    from app.storage.sqlite_store import MissionStore
    from app.tenants.admins import AdminStore
    from app.tenants.audit import AuditStore
    from app.tenants.clients import ClientStore

    db = tmp_path / "api.db"
    monkeypatch.setattr(main_module, "store", MissionStore(db))
    monkeypatch.setattr(main_module, "clients", ClientStore(db))
    monkeypatch.setattr(main_module, "admins", AdminStore(db))
    monkeypatch.setattr(main_module, "audit", AuditStore(db))

    yield main_module


def _oversize() -> bytes:
    limit = 256 * 1024

    return b"x" * (limit + 1024)


def test_http_declared_oversize_body_is_rejected(isolated_api):
    client = TestClient(app)

    response = client.post(
        "/api/v1/missions",
        content=_oversize(),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "request body too large"


def test_http_chunked_oversize_body_is_rejected(isolated_api):
    client = TestClient(app)

    # A generator body makes httpx use chunked transfer, so there is
    # no Content-Length for the server to consult.
    response = client.post(
        "/api/v1/missions",
        content=iter([_oversize()]),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "request body too large"


def test_http_streamed_oversize_body_in_many_chunks_is_rejected(isolated_api):
    client = TestClient(app)

    chunk = b"y" * (64 * 1024)

    response = client.post(
        "/api/v1/missions",
        content=iter([chunk] * 8),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413


def test_http_small_body_is_unaffected(isolated_api):
    client = TestClient(app)

    response = client.post(
        "/api/v1/missions",
        json={"goal": "small body", "capability": "code"},
    )

    assert response.status_code == 200
    assert response.json()["status"] in ("QUEUED", "EXECUTING", "PASS")


def test_http_rejection_carries_the_correlation_id(isolated_api):
    client = TestClient(app)

    response = client.post(
        "/api/v1/missions",
        content=_oversize(),
        headers={"content-type": "application/json", "X-Request-ID": "trace-me"},
    )

    assert response.status_code == 413
    assert response.headers["X-Request-ID"] == "trace-me"
