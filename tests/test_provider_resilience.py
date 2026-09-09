"""
Stage 8.6: provider resilience.

Bounded retries, exponential backoff with jitter, retry
classification, and the per-thread attempt log. All tests use
fake httpx transports; never a real model.
"""

import os
import types

import httpx
import pytest

from app.llm import provider as provider_module
from app.llm.provider import (
    LocalLLMProvider,
    LLMError,
    pop_attempt_log,
    provider_retry_config,
)


@pytest.fixture(autouse=True)
def fake_provider_env(monkeypatch):
    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "3")
    monkeypatch.setenv("YODAW_PROVIDER_BACKOFF_SECONDS", "0.01")
    monkeypatch.setenv("YODAW_LLM_MODEL", "fake-model")
    monkeypatch.setenv("YODAW_LLM_STYLE", "ollama")
    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "5")


def make_fake_httpx(handler):
    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError

    def post(url, json=None, headers=None, timeout=None):
        request = httpx.Request("POST", url)
        return handler(request)

    fake.post = post
    return fake


def run_chat(handler, monkeypatch):
    monkeypatch.setattr(
        provider_module, "httpx", make_fake_httpx(handler)
    )

    provider = LocalLLMProvider()
    provider.base_url = "http://fake"

    return provider._ollama("system", "user")


def test_provider_retry_config_defaults_and_parsing(monkeypatch):
    monkeypatch.delenv("YODAW_PROVIDER_MAX_RETRIES", raising=False)
    monkeypatch.delenv("YODAW_PROVIDER_BACKOFF_SECONDS", raising=False)

    assert provider_retry_config() == (3, 1.0)

    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("YODAW_PROVIDER_BACKOFF_SECONDS", "-5")

    max_retries, backoff = provider_retry_config()

    assert max_retries == 0
    assert backoff == 0.0

    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "not-a-number")

    assert provider_retry_config()[0] == 3


def test_transient_failure_recovers_with_backoff(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1

        if calls["n"] <= 2:
            raise httpx.ConnectError("connection refused", request=request)

        return httpx.Response(
            200,
            json={"message": {"content": "ok"}},
            request=request,
        )

    result = run_chat(handler, monkeypatch)

    assert result == "ok"
    assert calls["n"] == 3


def test_attempts_logged_for_evidence(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1

        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)

        return httpx.Response(
            200,
            json={"message": {"content": "ok"}},
            request=request,
        )

    run_chat(handler, monkeypatch)

    log = pop_attempt_log()
    kinds = [e.get("provider_attempt") for e in log]

    assert 1 in kinds and 2 in kinds
    assert any("provider_backoff" in e for e in log)

    # Log drains: second read is empty.
    assert pop_attempt_log() == []


def test_non_retryable_auth_failure_fails_fast(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={}, request=request)

    with pytest.raises(LLMError):
        run_chat(handler, monkeypatch)

    assert calls["n"] == 1, "401 must never be retried"


def test_retry_exhaustion_raises_after_max_attempts(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMError):
        run_chat(handler, monkeypatch)

    # 1 initial + 3 retries.
    assert calls["n"] == 4


def test_malformed_response_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"unexpected": True}, request=request)

    with pytest.raises(LLMError):
        run_chat(handler, monkeypatch)

    assert calls["n"] == 1


def test_5xx_is_retryable_4xx_is_not(monkeypatch):
    from app.llm.provider import _is_retryable

    def status_error(code):
        response = httpx.Response(code, request=httpx.Request("POST", "http://x"))
        return httpx.HTTPStatusError(
            f"{code}", request=response.request, response=response
        )

    assert _is_retryable(status_error(500)) is True
    assert _is_retryable(status_error(503)) is True
    assert _is_retryable(status_error(429)) is True
    assert _is_retryable(status_error(401)) is False
    assert _is_retryable(status_error(404)) is False
    assert _is_retryable(status_error(400)) is False
