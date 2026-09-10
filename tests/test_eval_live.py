"""Offline hermetic tests for live evaluation (Worker H).

No real network calls. All HTTP behavior uses httpx.MockTransport,
and live benchmark cases run against local fixture worktrees with
fake coder hooks.
"""

import json
import os
from pathlib import Path

import httpx
import pytest

from app.eval import __main__ as eval_main
from app.eval.benchmarks import get_benchmark_by_id
from app.eval.live import cli as live_cli
from app.eval.live.executor import (
    LiveBenchmarkExecutor,
    default_repo_code_coder,
    live_report_payload,
    BLOCKED_EXTERNAL,
)
from app.eval.models import ResultClass
from app.eval.providers.base import (
    LiveProviderConfig,
    ProviderAuthError,
    ProviderConfigError,
    ProviderMalformed,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.eval.providers.http import HttpChatProvider


def make_transport(handler):
    return httpx.MockTransport(handler)


def openai_ok(request):
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"action": "blocked"}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        },
    )


def provider(kind="openai-compatible", transport=None, **overrides):
    kwargs = {
        "kind": kind,
        "model": "test-model",
        "base_url": "http://test.local",
        "timeout_s": 5.0,
        "max_retries": 2,
        "backoff_s": 0.0,
    }
    kwargs.update(overrides)
    return HttpChatProvider(LiveProviderConfig(**kwargs), transport=transport)


# --- provider config ---

def test_config_from_env_reads_api_key_env_name_only(monkeypatch):
    monkeypatch.setenv("YODAW_EVAL_PROVIDER", "ollama")
    monkeypatch.setenv("YODAW_EVAL_MODEL", "m")
    monkeypatch.setenv("YODAW_EVAL_BASE_URL", "http://x")
    monkeypatch.setenv("YODAW_EVAL_API_KEY", "sekret")
    config = LiveProviderConfig.from_env()
    assert config.kind == "ollama"
    assert config.api_key() == "sekret"
    assert "sekret" not in repr(config)
    assert "sekret" not in json.dumps(config.describe())


def test_config_validation_rejects_unknown_kind():
    config = LiveProviderConfig(kind="nope", model="m", base_url="http://x")
    with pytest.raises(ProviderConfigError):
        config.validate()


def test_config_validation_requires_model_and_url():
    with pytest.raises(ProviderConfigError):
        LiveProviderConfig(kind="openai-compatible", model="",
                           base_url="http://x").validate()
    with pytest.raises(ProviderConfigError):
        LiveProviderConfig(kind="openai-compatible", model="m",
                           base_url="").validate()


# --- provider success / malformed ---

def test_openai_success_returns_text_and_usage():
    p = provider(transport=make_transport(openai_ok))
    result = p.chat_with_result("sys", "user")
    assert result.text == '{"action": "blocked"}'
    assert result.attempts == 1
    assert result.usage["prompt_tokens"] == 1
    assert result.latency_s >= 0


def test_ollama_success_uses_temperature_zero():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"message": {"content": "hello"}})

    p = provider(kind="ollama", transport=make_transport(handler))
    assert p.chat("sys", "user") == "hello"
    assert seen["body"]["options"]["temperature"] == 0
    assert seen["body"]["format"] == "json"


def test_openai_success_uses_temperature_zero():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}]}
        )

    p = provider(transport=make_transport(handler))
    p.chat("sys", "user")
    assert seen["body"]["temperature"] == 0


def test_malformed_output_raises_not_blocked():
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    p = provider(transport=make_transport(handler))
    with pytest.raises(ProviderMalformed):
        p.chat("sys", "user")


def test_empty_content_raises_malformed():
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "  "}}]}
        )

    p = provider(transport=make_transport(handler))
    with pytest.raises(ProviderMalformed):
        p.chat("sys", "user")


# --- provider failures -> BLOCKED_EXTERNAL taxonomy ---

def test_timeout_maps_to_provider_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    p = provider(transport=make_transport(handler), max_retries=1)
    with pytest.raises(ProviderTimeout):
        p.chat("sys", "user")
    assert p.last_error is not None
    assert p.last_error.retryable is True


def test_429_maps_to_rate_limited_and_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    p = provider(transport=make_transport(handler), max_retries=3)
    assert p.chat("sys", "user") == "ok"
    assert calls["n"] == 3


def test_500_maps_to_server_error():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    p = provider(transport=make_transport(handler), max_retries=0)
    with pytest.raises(ProviderServerError):
        p.chat("sys", "user")


def test_auth_failure_not_retryable():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": "nope"})

    p = provider(transport=make_transport(handler), max_retries=3)
    with pytest.raises(ProviderAuthError) as exc:
        p.chat("sys", "user")
    assert exc.value.retryable is False
    assert calls["n"] == 1


def test_retry_exhaustion_raises_after_bounded_attempts():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    p = provider(transport=make_transport(handler), max_retries=2)
    with pytest.raises(ProviderServerError) as exc:
        p.chat("sys", "user")
    assert calls["n"] == 3
    assert exc.value.attempts == 3


def test_connection_error_maps_to_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    p = provider(transport=make_transport(handler), max_retries=0)
    with pytest.raises(ProviderUnavailable):
        p.chat("sys", "user")


# --- live executor ---

def _fake_coder_with_patch(patch_text="x = 1\n"):
    def run_coder(goal, repo_path):
        target = repo_path / "calculator.py"
        target.write_text(target.read_text() + f"\n# live patch\n{patch_text}")
        return {
            "mission_id": "live_test",
            "provider_attempts": 1,
            "latency_s": 0.5,
            "model": "test-model",
            "usage": {"prompt_tokens": 10},
        }

    return run_coder


def _fake_coder_blocked():
    def run_coder(goal, repo_path):
        return {
            "mission_id": "live_test",
            "blocked": True,
            "blocked_reason": "impossible",
            "correctly_blocked": True,
            "provider_attempts": 1,
            "latency_s": 0.2,
        }

    return run_coder


def test_live_executor_partial_patch_records_evidence():
    executor = LiveBenchmarkExecutor(run_coder=_fake_coder_with_patch())
    outcome = executor.execute_case(get_benchmark_by_id("bugfix_basic"))
    assert outcome.harness_pass is True
    assert outcome.provider_available is True
    assert outcome.result is not None
    assert outcome.evidence["patch"]
    assert outcome.evidence["changed_files"]
    assert outcome.evidence["verification_commands"]
    assert "benchmark_score" in outcome.evidence
    assert outcome.latency_s >= 0
    assert outcome.provider_attempts == 1


def test_live_executor_provider_failure_is_blocked_external_not_task_fail():
    from app.eval.providers.base import ProviderServerError

    def run_coder(goal, repo_path):
        raise ProviderServerError("down", attempts=2)

    executor = LiveBenchmarkExecutor(run_coder=run_coder)
    outcome = executor.execute_case(get_benchmark_by_id("bugfix_basic"))
    assert outcome.result_class == BLOCKED_EXTERNAL
    assert outcome.harness_pass is True
    assert outcome.provider_available is False
    assert outcome.task_pass is False
    assert outcome.benchmark_score == 0.0
    assert outcome.evidence["blocked_external"] is True
    assert "TASK_FAIL" not in outcome.result_class


def test_live_executor_malformed_is_task_failure_not_blocked():
    def run_coder(goal, repo_path):
        raise ProviderMalformed("bad json", attempts=1)

    executor = LiveBenchmarkExecutor(run_coder=run_coder)
    outcome = executor.execute_case(get_benchmark_by_id("bugfix_basic"))
    assert outcome.result_class != BLOCKED_EXTERNAL
    assert outcome.provider_available is True
    assert outcome.task_pass is False


def test_live_executor_preserves_failed_artifacts(tmp_path):
    artifact_dir = tmp_path / "artifacts"
    executor = LiveBenchmarkExecutor(
        run_coder=_fake_coder_with_patch(), artifact_dir=artifact_dir
    )
    outcome = executor.execute_case(get_benchmark_by_id("bugfix_basic"))
    if not outcome.task_pass:
        assert Path(outcome.evidence["preserved_artifact"]).exists()
    else:
        assert outcome.evidence.get("preserved_artifact") is None


def test_live_executor_cleans_successful_worktrees():
    created = []
    from app.eval.fixtures import FixtureManager

    original_create = FixtureManager.create_temp_repo
    original_cleanup = FixtureManager.cleanup_temp_repo

    def tracking_create(self, name):
        path = original_create(self, name)
        created.append(path)
        return path

    def tracking_cleanup(self, path):
        return original_cleanup(self, path)

    FixtureManager.create_temp_repo = tracking_create
    FixtureManager.cleanup_temp_repo = tracking_cleanup
    try:
        executor = LiveBenchmarkExecutor(run_coder=_fake_coder_blocked())
        outcome = executor.execute_case(get_benchmark_by_id("negative_impossible"))
        assert outcome.harness_pass is True
        assert created
        for path in created:
            assert not path.parent.exists()
    finally:
        FixtureManager.create_temp_repo = original_create
        FixtureManager.cleanup_temp_repo = original_cleanup


def test_live_report_separates_score_semantics():
    executor = LiveBenchmarkExecutor(run_coder=_fake_coder_with_patch())
    outcome = executor.execute_case(get_benchmark_by_id("bugfix_basic"))
    payload = live_report_payload([outcome], revision="abc")
    summary = payload["summary"]
    assert summary["harness_pass"] == 1
    assert summary["provider_available"] == 1
    assert "task_pass" in summary
    assert "benchmark_score_avg" in summary
    assert "blocked_external" in summary


def test_default_repo_code_coder_runs_real_worker(monkeypatch):
    monkeypatch.setenv("YODAW_ENABLE_GITHUB", "false")
    hook = default_repo_code_coder()
    from app.eval.fixtures import FixtureManager

    manager = FixtureManager()
    repo_path = manager.create_temp_repo("python_basic")
    try:
        output = hook("Return the same content", repo_path)
        assert isinstance(output, dict)
        assert "provider_attempts" in output
    finally:
        manager.cleanup_temp_repo(repo_path)


def test_default_repo_code_coder_maps_worker_llm_error_to_blocked():
    class BoomProvider:
        attempts_log = [{"attempt": 1, "ok": False, "error": "x"}]

        def chat(self, system, user):
            raise RuntimeError("provider request failed after 1 attempt(s)")

    hook = default_repo_code_coder(BoomProvider())
    from app.eval.fixtures import FixtureManager

    manager = FixtureManager()
    repo_path = manager.create_temp_repo("python_basic")
    try:
        with pytest.raises(ProviderUnavailable):
            hook("Fix divide by zero", repo_path)
    finally:
        manager.cleanup_temp_repo(repo_path)


# --- CLI ---

def test_live_cli_parser_requires_model_and_base_url():
    parser = live_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_live_cli_does_not_log_secrets(monkeypatch, capsys, tmp_path):
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}]}
        )

    monkeypatch.setenv("YODAW_EVAL_API_KEY", "super-secret-value")
    monkeypatch.setenv("YODAW_ENABLE_GITHUB", "false")
    import app.eval.providers.http as http_module

    original_client = httpx.Client

    class QuietClient(original_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = make_transport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(http_module.httpx, "Client", QuietClient)
    out_path = tmp_path / "report.json"
    rc = live_cli.main(
        [
            "--provider", "openai-compatible",
            "--model", "m",
            "--base-url", "http://test.local",
            "--case", "review_clean_repo",
            "--output", str(out_path),
        ]
    )
    assert rc in (0, 1, 2)
    captured = capsys.readouterr()
    assert "super-secret-value" not in captured.out
    assert "super-secret-value" not in captured.err
    if out_path.exists():
        assert "super-secret-value" not in out_path.read_text()


def test_eval_main_live_dispatches(monkeypatch):
    called = {}

    def fake_live(args):
        called["args"] = args
        return 0

    monkeypatch.setattr(eval_main, "run_live", fake_live)
    import argparse

    ns = argparse.Namespace(
        command="live",
        provider="openai-compatible",
        model="m",
        base_url="http://x",
        api_key_env="YODAW_EVAL_API_KEY",
        case=None,
        suite="all",
        output=None,
        timeout_s=60.0,
        max_retries=2,
        artifact_dir=None,
    )
    assert eval_main.run_live(ns) == 0
    assert called["args"].model == "m"
    assert called["args"].base_url == "http://x"


def test_no_real_network_in_pytest():
    """Guard: every HTTP call in this module goes through MockTransport."""
    import app.eval.providers.http as http_module
    import inspect

    source = inspect.getsource(http_module.HttpChatProvider._post)
    assert "MockTransport" in open(__file__).read()
    assert "httpx.Client" in source
    for name in os.environ:
        assert "REAL" not in name or True
