"""
Route failover + prepare-failure repair.

- 9Router style: YODAW_LLM_FALLBACK_MODELS rotates the route on
  every retryable failure (timeout/5xx) instead of hammering one
  dead route for the whole retry budget.
- Repo worker: an LLM plan that cannot be applied
  (FindTextMissing/TargetNotFound/TargetNotFile) spends retry
  budget on a corrective repair plan with the application error
  fed back, instead of failing the mission on a near-miss plan.

All tests use fake transports/plans; never a real model.
"""

import subprocess
import types

import httpx
import pytest

import app.workers.repo_code_worker as worker_module
from app.llm import provider as provider_module
from app.llm.provider import (
    LocalLLMProvider,
    fallback_models,
    pop_attempt_log,
)
from app.workers.repo_code_worker import RepoCodeWorker


# ----------------------------------------------------------
# 9Router route failover
# ----------------------------------------------------------


def make_capturing_httpx(handler, captured):
    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError

    def post(url, json=None, headers=None, timeout=None):
        captured.append(json)
        request = httpx.Request("POST", url)
        return handler(request)

    fake.post = post
    return fake


def ok_response(request, content="ok"):
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
        request=request,
    )


def error_503(request):
    raise httpx.HTTPStatusError(
        "service unavailable",
        request=request,
        response=httpx.Response(503, request=request),
    )


@pytest.fixture
def failover_env(monkeypatch):
    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "3")
    monkeypatch.setenv("YODAW_PROVIDER_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv(
        "YODAW_LLM_FALLBACK_MODELS",
        "route-fallback-1, route-fallback-2",
    )
    pop_attempt_log()
    yield
    pop_attempt_log()


def run_ninerouter_chat(handler, monkeypatch, captured):
    monkeypatch.setattr(
        provider_module,
        "httpx",
        make_capturing_httpx(handler, captured),
    )

    provider = LocalLLMProvider(
        style="9router",
        base_url="http://fake",
        model="route-primary",
        api_key="key",
    )

    return provider._ninerouter("system", "user")


def test_fallback_models_parsing(monkeypatch):
    monkeypatch.setenv(
        "YODAW_LLM_FALLBACK_MODELS",
        " b ,a,, b ,",
    )

    assert fallback_models() == ["b", "a"]

    monkeypatch.delenv("YODAW_LLM_FALLBACK_MODELS", raising=False)

    assert fallback_models() == []


def test_retryable_error_fails_over_to_next_route(
    monkeypatch, failover_env
):
    captured = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1

        if calls["n"] == 1:
            error_503(request)

        return ok_response(request)

    result = run_ninerouter_chat(handler, monkeypatch, captured)

    assert result == "ok"
    assert [item["model"] for item in captured] == [
        "route-primary",
        "route-fallback-1",
    ]

    log = pop_attempt_log()
    hops = [
        entry
        for entry in log
        if entry.get("provider_model_fallback")
    ]

    assert len(hops) == 1
    assert hops[0]["from"] == "route-primary"
    assert hops[0]["to"] == "route-fallback-1"


def test_timeout_fails_over_to_next_route(monkeypatch, failover_env):
    captured = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1

        if calls["n"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)

        return ok_response(request)

    result = run_ninerouter_chat(handler, monkeypatch, captured)

    assert result == "ok"
    assert [item["model"] for item in captured] == [
        "route-primary",
        "route-fallback-1",
    ]


def test_chain_sticks_to_last_fallback(monkeypatch, failover_env):
    captured = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1

        if calls["n"] <= 3:
            error_503(request)

        return ok_response(request)

    result = run_ninerouter_chat(handler, monkeypatch, captured)

    assert result == "ok"
    assert [item["model"] for item in captured] == [
        "route-primary",
        "route-fallback-1",
        "route-fallback-2",
        "route-fallback-2",
    ]


def test_no_fallbacks_repeats_primary(monkeypatch, failover_env):
    monkeypatch.delenv("YODAW_LLM_FALLBACK_MODELS", raising=False)

    captured = []
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1

        if calls["n"] == 1:
            error_503(request)

        return ok_response(request)

    result = run_ninerouter_chat(handler, monkeypatch, captured)

    assert result == "ok"
    assert [item["model"] for item in captured] == [
        "route-primary",
        "route-primary",
    ]

    log = pop_attempt_log()

    assert not [
        entry
        for entry in log
        if entry.get("provider_model_fallback")
    ]


def test_attempt_log_records_route_per_attempt(
    monkeypatch, failover_env
):
    captured = []

    def handler(request):
        return ok_response(request)

    run_ninerouter_chat(handler, monkeypatch, captured)

    log = pop_attempt_log()
    attempts = [
        entry
        for entry in log
        if entry.get("provider_attempt") == 1
    ]

    assert attempts
    assert attempts[0]["model"] == "route-primary"


def test_total_attempts_bounded_by_max_retries(
    monkeypatch,
):
    """Retry amplification ceiling: a permanently dead 9Router with
    a route chain must stop after exactly max_retries+1 attempts —
    never one attempt per route times retries."""
    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "2")
    monkeypatch.setenv("YODAW_PROVIDER_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv(
        "YODAW_LLM_FALLBACK_MODELS",
        "route-b,route-c,route-d",
    )

    captured = []
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.HTTPStatusError(
            "down",
            request=request,
            response=httpx.Response(503, request=request),
        )

    with pytest.raises(Exception):
        run_ninerouter_chat(handler, monkeypatch, captured)

    # 3 routes in the chain, max_retries=2 => at most 3 posts total
    # (attempt 1..3), never 3 routes x 3 retries = 9.
    assert len(attempts) == 3, f"amplified retries: {len(attempts)}"
    # The chain must rotate routes, not hammer route-primary blindly.
    assert [j["model"] for j in captured] == [
        "route-primary",
        "route-b",
        "route-c",
    ]

    pop_attempt_log()


# ----------------------------------------------------------
# Worker repair on unappliable LLM plans
# ----------------------------------------------------------


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repo(repo, files):
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    for name, content in files.items():
        (repo / name).write_text(content)

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")


@pytest.fixture
def greet_repo(tmp_path):
    repo = tmp_path / "repo"

    init_repo(
        repo,
        {
            "app.py": "def greet():\n    return 'hi'\n",
            "test_app.py": (
                "from app import greet\n\n\n"
                "def test_greet():\n"
                "    assert greet() == 'hello'\n"
            ),
            "pytest.ini": "[pytest]\npythonpath = .\n",
        },
    )

    return repo


BAD_FIND_PLAN = {
    "action": "edit",
    "edits": [
        {
            "target_file": "app.py",
            # Flattened single line: the file has two lines, so
            # prepare fails with FindTextMissing.
            "find": "def greet(): return 'hi'",
            "replace": "def greet(): return 'hello'",
        }
    ],
    "reason": "near-miss plan with wrong find text",
}

GOOD_PLAN = {
    "action": "edit",
    "edits": [
        {
            "target_file": "app.py",
            "find": "def greet():\n    return 'hi'\n",
            "replace": "def greet():\n    return 'hello'\n",
        }
    ],
    "reason": "exact find text",
}


def make_repair_llm(monkeypatch, plans, seen):
    calls = {"plan": 0, "repair": 0}

    def fake_generate_edit_plan(goal, worktree, lessons=""):
        plan = plans[0]
        calls["plan"] += 1
        return plan

    def fake_generate_repair_plan(
        goal,
        worktree,
        previous_plan,
        failure_context,
        provider=None,
        lessons="",
    ):
        seen.append(failure_context)
        plan = plans[1 + calls["repair"]]
        calls["repair"] += 1
        return plan

    monkeypatch.setattr(
        worker_module,
        "generate_edit_plan",
        fake_generate_edit_plan,
    )
    monkeypatch.setattr(
        worker_module,
        "generate_repair_plan",
        fake_generate_repair_plan,
    )


def test_prepare_failure_triggers_repair_and_passes(
    monkeypatch, greet_repo
):
    seen = []
    make_repair_llm(
        monkeypatch, [BAD_FIND_PLAN, GOOD_PLAN], seen
    )

    result = RepoCodeWorker().execute(
        "Make greet return hello.",
        {
            "repo_path": str(greet_repo),
            "branch_name": "yodaw/test-prepare-repair",
            "max_retries": 1,
        },
    )

    assert result["success"] is True, result["error"]
    assert result["output"]["retries"] == 1
    assert result["output"]["attempts"] == 2

    kinds = [
        item.get("type")
        for item in result["evidence"]
        if isinstance(item, dict)
    ]

    assert "prepare_failure" in kinds
    assert "repair_plan" in kinds

    assert len(seen) == 1
    assert seen[0]["prepare_error"]["error"]["type"] == (
        "FindTextMissing"
    )


def test_prepare_retry_exhausted_after_failed_repair(
    monkeypatch, greet_repo
):
    seen = []
    make_repair_llm(
        monkeypatch, [BAD_FIND_PLAN, BAD_FIND_PLAN], seen
    )

    result = RepoCodeWorker().execute(
        "Make greet return hello.",
        {
            "repo_path": str(greet_repo),
            "branch_name": "yodaw/test-prepare-exhausted",
            "max_retries": 1,
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "RetryExhausted"
    assert result["retryable"] is True
    assert result["error"]["prepare_error"]["type"] == (
        "FindTextMissing"
    )


def test_path_escape_still_fails_closed(monkeypatch, greet_repo):
    escape_plan = {
        "action": "edit",
        "edits": [
            {
                "target_file": "../escape.py",
                "find": "x",
                "replace": "y",
            }
        ],
    }
    seen = []
    make_repair_llm(monkeypatch, [escape_plan, GOOD_PLAN], seen)

    result = RepoCodeWorker().execute(
        "Make greet return hello.",
        {
            "repo_path": str(greet_repo),
            "branch_name": "yodaw/test-escape-closed",
            "max_retries": 2,
        },
    )

    assert result["success"] is False
    assert result["error"]["type"] == "PathEscapeError"
    assert result["retryable"] is False
    assert seen == []


# ----------------------------------------------------------
# Repair prompt carries the application failure
# ----------------------------------------------------------


def test_repair_prompt_includes_prepare_error(tmp_path):
    from app.llm.coder import generate_repair_plan

    (tmp_path / "app.py").write_text("x = 1\n")

    captured = {}

    class FakeProvider:
        def chat(self, system, user):
            captured["system"] = system
            captured["user"] = user

            return (
                '{"action": "edit", "edits": [{"target_file": '
                '"app.py", "find": "x = 1", '
                '"replace": "x = 2"}]}'
            )

    generate_repair_plan(
        "goal",
        tmp_path,
        {"action": "edit", "edits": []},
        {
            "attempt": 1,
            "tests": [],
            "diff": "",
            "touched_files": ["app.py"],
            "prepare_error": {
                "error": {
                    "type": "FindTextMissing",
                    "message": "nope",
                }
            },
        },
        provider=FakeProvider(),
        lessons=" ",
    )

    assert "EDIT APPLICATION FAILURE" in captured["user"]
    assert "FindTextMissing" in captured["user"]


def test_repair_prompt_unchanged_without_prepare_error(tmp_path):
    from app.llm.coder import generate_repair_plan

    (tmp_path / "app.py").write_text("x = 1\n")

    captured = {}

    class FakeProvider:
        def chat(self, system, user):
            captured["user"] = user

            return (
                '{"action": "edit", "edits": [{"target_file": '
                '"app.py", "find": "x = 1", '
                '"replace": "x = 2"}]}'
            )

    generate_repair_plan(
        "goal",
        tmp_path,
        {"action": "edit", "edits": []},
        {
            "attempt": 1,
            "tests": [{"cmd": "pytest", "returncode": 1}],
            "diff": "a",
            "touched_files": ["app.py"],
        },
        provider=FakeProvider(),
        lessons=" ",
    )

    assert "EDIT APPLICATION FAILURE" not in captured["user"]
    assert "failed validation" in captured["user"]
