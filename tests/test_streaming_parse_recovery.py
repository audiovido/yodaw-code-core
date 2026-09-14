"""
Streaming SSE support + bounded plan-parse recovery.

- 9Router/OpenAI styles: YODAW_LLM_STREAM=1 accumulates SSE
  chunks (delta.content or message.content) until data: [DONE].
  Transport failures mid-stream are retryable; HTTP errors keep
  the Stage 8.6 classification; model failover keeps rotating.
- Coder: unparseable plans get bounded re-prompts carrying the
  previous raw output plus the parse error (PlanParseError after
  exhaustion), and the worker records plan_parse_failure
  evidence instead of a bare LLMError.

All tests use fake transports/providers; never a real model.
"""

import json
import subprocess
import types

import httpx
import pytest

import app.workers.repo_code_worker as worker_module
from app.llm import provider as provider_module
from app.llm.provider import (
    LocalLLMProvider,
    LLMError,
    accumulate_openai_stream,
    llm_stream_enabled,
    pop_attempt_log,
)
from app.workers.repo_code_worker import RepoCodeWorker


# ----------------------------------------------------------
# Fakes
# ----------------------------------------------------------


def sse_data(obj):
    return "data: " + json.dumps(obj)


SSE_DONE = "data: [DONE]"


def delta_chunk(text):
    return sse_data({"choices": [{"delta": {"content": text}}]})


def message_chunk(text):
    return sse_data({"choices": [{"message": {"content": text}}]})


class FakeStreamResponse:
    """Minimal httpx streaming response (context manager)."""

    def __init__(self, status=200, lines=(), fail_after=None):
        self._status = status
        self._lines = list(lines)
        self._fail_after = fail_after
        self.request = httpx.Request("POST", "http://fake")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError(
                f"status {self._status}",
                request=self.request,
                response=httpx.Response(
                    self._status, request=self.request
                ),
            )

    def iter_lines(self):
        for index, line in enumerate(self._lines):
            if (
                self._fail_after is not None
                and index >= self._fail_after
            ):
                raise httpx.StreamError("connection broke")
            yield line


def make_streaming_httpx(respond, captured):
    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError
    fake.StreamError = httpx.StreamError

    def post(url, json=None, headers=None, timeout=None):
        raise AssertionError("single-shot post used in stream mode")

    def stream(method, url, json=None, headers=None, timeout=None):
        captured.append(json)
        return respond()

    fake.post = post
    fake.stream = stream
    return fake


@pytest.fixture
def stream_env(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STREAM", "1")
    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "3")
    monkeypatch.setenv("YODAW_PROVIDER_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "5")
    monkeypatch.delenv("YODAW_LLM_FALLBACK_MODELS", raising=False)
    pop_attempt_log()
    yield
    pop_attempt_log()


def run_stream_chat(respond, monkeypatch, captured, model="r-primary"):
    monkeypatch.setattr(
        provider_module,
        "httpx",
        make_streaming_httpx(respond, captured),
    )

    provider = LocalLLMProvider(
        style="9router",
        base_url="http://fake",
        model=model,
        api_key="key",
    )

    return provider._ninerouter("system", "user")


# ----------------------------------------------------------
# SSE accumulation
# ----------------------------------------------------------


def test_llm_stream_enabled_parsing(monkeypatch):
    monkeypatch.delenv("YODAW_LLM_STREAM", raising=False)

    assert llm_stream_enabled() is False

    for value in ("1", "true", "TRUE", " yes ", "on"):
        monkeypatch.setenv("YODAW_LLM_STREAM", value)

        assert llm_stream_enabled() is True

    monkeypatch.setenv("YODAW_LLM_STREAM", "0")

    assert llm_stream_enabled() is False


def test_stream_accumulates_deltas_and_shapes():
    response = FakeStreamResponse(
        lines=[
            ": ping",
            "",
            delta_chunk("Hel"),
            "  " + delta_chunk("lo") + "  ",
            message_chunk("!"),
            SSE_DONE,
            delta_chunk("after-done-ignored"),
        ]
    )

    content, chunks = accumulate_openai_stream(response)

    assert content == "Hello!"
    assert chunks == 3


def test_stream_bytes_lines_and_malformed_frames():
    response = FakeStreamResponse(
        lines=[
            b"data: {not-json",
            "not-a-data-line",
            delta_chunk("ok"),
            SSE_DONE,
        ]
    )

    content, chunks = accumulate_openai_stream(response)

    assert content == "ok"
    assert chunks == 1


def test_stream_error_object_raises():
    response = FakeStreamResponse(
        lines=[
            delta_chunk("part"),
            sse_data({"error": {"message": "boom"}}),
        ]
    )

    with pytest.raises(LLMError, match="streamed chat error"):
        accumulate_openai_stream(response)


def test_stream_empty_raises():
    response = FakeStreamResponse(lines=[SSE_DONE])

    with pytest.raises(LLMError, match="empty"):
        accumulate_openai_stream(response)


# ----------------------------------------------------------
# Streaming retry + failover behavior
# ----------------------------------------------------------


def test_stream_success_records_evidence(monkeypatch, stream_env):
    captured = []

    def respond():
        return FakeStreamResponse(
            lines=[delta_chunk("hi"), SSE_DONE]
        )

    result = run_stream_chat(respond, monkeypatch, captured)

    assert result == "hi"
    assert captured[0]["stream"] is True
    assert captured[0]["model"] == "r-primary"

    log = pop_attempt_log()
    attempts = [
        entry
        for entry in log
        if entry.get("provider_attempt") == 1
    ]

    assert attempts
    assert attempts[0]["stream"] is True
    assert attempts[0]["stream_chunks"] == 1
    assert attempts[0]["status"] == 200


def test_stream_http_503_retries_then_succeeds(
    monkeypatch, stream_env
):
    captured = []
    calls = {"n": 0}

    def respond():
        calls["n"] += 1

        if calls["n"] == 1:
            return FakeStreamResponse(status=503)

        return FakeStreamResponse(lines=[delta_chunk("ok"), SSE_DONE])

    result = run_stream_chat(respond, monkeypatch, captured)

    assert result == "ok"
    assert len(captured) == 2


def test_stream_mid_transport_break_retries(
    monkeypatch, stream_env
):
    captured = []
    calls = {"n": 0}

    def respond():
        calls["n"] += 1

        if calls["n"] == 1:
            return FakeStreamResponse(
                lines=[delta_chunk("par"), delta_chunk("t")],
                fail_after=1,
            )

        return FakeStreamResponse(lines=[delta_chunk("ok"), SSE_DONE])

    result = run_stream_chat(respond, monkeypatch, captured)

    assert result == "ok"
    assert len(captured) == 2

    log = pop_attempt_log()
    first = [
        entry
        for entry in log
        if entry.get("provider_attempt") == 1
    ][0]

    assert first["retryable"] is True


def test_stream_empty_does_not_retry(monkeypatch, stream_env):
    captured = []
    calls = {"n": 0}

    def respond():
        calls["n"] += 1
        return FakeStreamResponse(lines=[SSE_DONE])

    with pytest.raises(LLMError, match="after 1 attempt"):
        run_stream_chat(respond, monkeypatch, captured)

    assert calls["n"] == 1


def test_stream_failover_rotates_route(monkeypatch, stream_env):
    monkeypatch.setenv(
        "YODAW_LLM_FALLBACK_MODELS", "r-fallback"
    )

    captured = []
    calls = {"n": 0}

    def respond():
        calls["n"] += 1

        if calls["n"] == 1:
            return FakeStreamResponse(status=502)

        return FakeStreamResponse(lines=[delta_chunk("ok"), SSE_DONE])

    result = run_stream_chat(respond, monkeypatch, captured)

    assert result == "ok"
    assert [item["model"] for item in captured] == [
        "r-primary",
        "r-fallback",
    ]
    assert all(item["stream"] is True for item in captured)

    log = pop_attempt_log()

    assert any(
        entry.get("provider_model_fallback") for entry in log
    )


def test_stream_response_closes_on_parser_raise(monkeypatch, stream_env):
    """_post_stream_text must close the stream deterministically even
    when the frame parser raises mid-stream (no reliance on GC)."""
    state = {"exited": False}
    captured = []

    class ClosingStream(FakeStreamResponse):
        def __exit__(self, *args):
            state["exited"] = True
            return False

    def respond():
        return ClosingStream(lines=[SSE_DONE])  # parser sees no content

    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError
    fake.StreamError = httpx.StreamError

    def stream(method, url, json=None, headers=None, timeout=None):
        captured.append(json)
        return respond()

    fake.stream = stream
    monkeypatch.setattr(provider_module, "httpx", fake)
    pop_attempt_log()

    provider = LocalLLMProvider(
        style="9router",
        base_url="http://fake",
        model="r-primary",
        api_key="key",
    )

    # Empty body raises LLMError from the accumulator...
    with pytest.raises(LLMError):
        provider._ninerouter("system", "user")

    # ...but the response handle was still closed deterministically.
    assert state["exited"] is True


def test_stream_without_support_is_terminal(monkeypatch, stream_env):
    captured = []
    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError
    fake.post = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("post used")
    )
    # Deliberately no .stream attribute.
    monkeypatch.setattr(provider_module, "httpx", fake)

    provider = LocalLLMProvider(
        style="9router",
        base_url="http://fake",
        model="r-primary",
        api_key="key",
    )

    with pytest.raises(LLMError, match="no stream support"):
        provider._ninerouter("system", "user")


def test_non_stream_default_uses_single_shot(monkeypatch):
    monkeypatch.delenv("YODAW_LLM_STREAM", raising=False)
    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "0")

    captured = []
    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError

    def post(url, json=None, headers=None, timeout=None):
        captured.append(json)
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}]},
            request=request,
        )

    fake.post = post
    monkeypatch.setattr(provider_module, "httpx", fake)
    pop_attempt_log()

    provider = LocalLLMProvider(
        style="9router",
        base_url="http://fake",
        model="r-primary",
        api_key="key",
    )

    assert provider._ninerouter("system", "user") == "ok"
    assert captured[0]["stream"] is False

    log = pop_attempt_log()

    assert "stream" not in log[0]


# ----------------------------------------------------------
# Parse recovery
# ----------------------------------------------------------


VALID_PLAN_JSON = (
    '{"action": "edit", "edits": [{"target_file": "app.py", '
    '"find": "a", "replace": "b"}]}'
)


class ScriptedProvider:
    def __init__(self, script):
        self.script = list(script)
        self.users = []

    def chat(self, system, user):
        self.users.append(user)
        action = self.script.pop(0)

        if isinstance(action, Exception):
            raise action

        return action


def test_parse_recovery_succeeds_on_reprompt(monkeypatch):
    from app.llm.coder import chat_for_plan

    monkeypatch.delenv("YODAW_CODER_PARSE_RETRIES", raising=False)

    provider = ScriptedProvider(["just prose, no json", VALID_PLAN_JSON])

    plan = chat_for_plan(provider, "sys", "make plan")

    assert plan["action"] == "edit"
    assert len(provider.users) == 2
    assert provider.users[0] == "make plan"
    assert "just prose, no json" in provider.users[1]
    assert "ONLY a single JSON object" in provider.users[1]


def test_parse_always_invalid_raises_with_bounds(monkeypatch):
    from app.llm.coder import PlanParseError, chat_for_plan

    monkeypatch.delenv("YODAW_CODER_PARSE_RETRIES", raising=False)

    raw = "x" * 5000
    provider = ScriptedProvider([raw] * 4)

    with pytest.raises(PlanParseError) as exc_info:
        chat_for_plan(provider, "sys", "make plan")

    assert len(provider.users) == 3
    assert exc_info.value.attempts == 3
    assert len(exc_info.value.raw_snippet) == 2000
    assert isinstance(exc_info.value, LLMError)


def test_parse_retries_zero_means_single_attempt(monkeypatch):
    from app.llm.coder import PlanParseError, chat_for_plan

    monkeypatch.setenv("YODAW_CODER_PARSE_RETRIES", "0")

    provider = ScriptedProvider(["nope", VALID_PLAN_JSON])

    with pytest.raises(PlanParseError):
        chat_for_plan(provider, "sys", "make plan")

    assert len(provider.users) == 1


def test_parse_recovery_config_parsing(monkeypatch):
    from app.llm.coder import parse_recovery_config

    monkeypatch.delenv("YODAW_CODER_PARSE_RETRIES", raising=False)

    assert parse_recovery_config() == 2

    monkeypatch.setenv("YODAW_CODER_PARSE_RETRIES", "bogus")

    assert parse_recovery_config() == 2

    monkeypatch.setenv("YODAW_CODER_PARSE_RETRIES", "-4")

    assert parse_recovery_config() == 0


def test_transport_error_is_not_parse_retried():
    from app.llm.coder import chat_for_plan

    provider = ScriptedProvider([LLMError("down")])

    with pytest.raises(LLMError, match="down"):
        chat_for_plan(provider, "sys", "make plan")

    assert len(provider.users) == 1


# ----------------------------------------------------------
# Worker surfaces parse exhaustion honestly
# ----------------------------------------------------------


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def greet_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    (repo / "app.py").write_text("def greet():\n    return 'hi'\n")
    (repo / "test_app.py").write_text(
        "from app import greet\n\n\n"
        "def test_greet():\n"
        "    assert greet() == 'hello'\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return repo


def test_worker_plan_parse_failure_terminal(monkeypatch, greet_repo):
    from app.llm.coder import PlanParseError

    def fake_generate_edit_plan(goal, worktree, lessons=""):
        raise PlanParseError(
            "Coder plan stayed unparseable after 3 attempt(s)",
            attempts=3,
            raw_snippet="nope",
        )

    monkeypatch.setattr(
        worker_module,
        "generate_edit_plan",
        fake_generate_edit_plan,
    )

    result = RepoCodeWorker().execute(
        "Make greet return hello.",
        {
            "repo_path": str(greet_repo),
            "branch_name": "yodaw/test-parse-terminal",
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "PlanParseError"
    assert result["error"]["parse_attempts"] == 3
    assert result["retryable"] is True

    kinds = [
        item.get("type")
        for item in result["evidence"]
        if isinstance(item, dict)
    ]

    assert "plan_parse_failure" in kinds


# ----------------------------------------------------------
# Ollama-native (NDJSON) streaming
# ----------------------------------------------------------

from app.llm.provider import accumulate_ollama_stream  # noqa: E402


def ndjson_line(obj):
    return json.dumps(obj)


def test_ollama_stream_accumulates_ndjson():
    response = FakeStreamResponse(
        lines=[
            "",
            "{oops",
            ndjson_line(
                {"message": {"content": "Hel"}, "done": False}
            ),
            ndjson_line(
                {"message": {"content": "lo"}, "done": False}
            ),
            ndjson_line({"done": True}),
        ]
    )

    content, chunks = accumulate_ollama_stream(response)

    assert content == "Hello"
    assert chunks == 2


def test_ollama_stream_stops_at_done():
    response = FakeStreamResponse(
        lines=[
            ndjson_line({"message": {"content": "a"}, "done": True}),
            ndjson_line({"message": {"content": "b"}}),
        ]
    )

    content, chunks = accumulate_ollama_stream(response)

    assert content == "a"
    assert chunks == 1


def test_ollama_stream_error_object_raises():
    response = FakeStreamResponse(
        lines=[ndjson_line({"error": "kaput"})]
    )

    with pytest.raises(LLMError, match="streamed Ollama error"):
        accumulate_ollama_stream(response)


def test_ollama_stream_empty_raises():
    response = FakeStreamResponse(lines=[ndjson_line({"done": True})])

    with pytest.raises(LLMError, match="empty"):
        accumulate_ollama_stream(response)


def test_ollama_style_streams_when_enabled(monkeypatch, stream_env):
    captured = []

    def respond():
        return FakeStreamResponse(
            lines=[
                ndjson_line(
                    {"message": {"content": "hi"}, "done": False}
                ),
                ndjson_line({"done": True}),
            ]
        )

    monkeypatch.setattr(
        provider_module,
        "httpx",
        make_streaming_httpx(respond, captured),
    )

    provider = LocalLLMProvider(
        style="ollama",
        base_url="http://fake",
        model="m",
    )

    assert provider._ollama("system", "user") == "hi"
    assert captured[0]["stream"] is True

    log = pop_attempt_log()

    assert log[0]["stream"] is True
    assert log[0]["stream_chunks"] == 1
