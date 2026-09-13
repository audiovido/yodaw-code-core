"""
Native 9Router acceptance: config, provider, inventory, fallback,
SQLite repair, CLI provisioning, and a hermetic YODAW->9Router E2E.

Everything here is hermetic: httpx is monkeypatched with fakes, the
config file points at tmp paths, and no subprocess or network call
ever leaves the test process.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from app.config import ConfigError
from app.llm import ninerouter as ninerouter_module
from app.llm import provider as provider_module
from app.llm.provider import (
    LLMError,
    LocalLLMProvider,
    fallback_styles,
    pop_attempt_log,
)
from app.product_config import (
    load_product_config,
    write_llm_config,
)


_ENV_NAMES = (
    "YODAW_CONFIG",
    "YODAW_LLM_PROVIDER",
    "YODAW_LLM_STYLE",
    "YODAW_LLM_MODE",
    "YODAW_LLM_MODEL",
    "YODAW_LLM_BASE_URL",
    "YODAW_LLM_API_KEY",
    "YODAW_LLM_API_KEY_ENV",
    "YODAW_LLM_FALLBACKS",
    "YODAW_PROVIDER_MAX_RETRIES",
    "YODAW_PROVIDER_BACKOFF_SECONDS",
    "YODAW_LLM_TIMEOUT_SECONDS",
    "YODAW_LLM_KEEP_ALIVE",
    "YODAW_ENABLE_GITHUB",
    "YODAW_PROFILE",
    "YODAW_HOST",
    "YODAW_PORT",
    "YODAW_LOG_LEVEL",
    "NINEROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def hermetic_env(tmp_path, monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("YODAW_PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("YODAW_PROVIDER_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("YODAW_ENABLE_GITHUB", "false")
    ninerouter_module.clear_model_cache()
    pop_attempt_log()
    return tmp_path


# ------------------------------------------------------------ fakes


def _response(payload, status=200, method="GET", url="http://fake/"):
    request = httpx.Request(method, url)
    return httpx.Response(status, json=payload, request=request)


class FakeHttpx:
    TimeoutException = httpx.TimeoutException
    ConnectError = httpx.ConnectError
    HTTPStatusError = httpx.HTTPStatusError

    def __init__(self, get=None, post=None):
        self._get = get
        self._post = post
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append(
            {"url": url, "headers": dict(headers or {}), "timeout": timeout}
        )
        assert self._get is not None, "unexpected GET"
        return self._get(url, headers, timeout)

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append(
            {
                "url": url,
                "json": json,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        assert self._post is not None, "unexpected POST"
        return self._post(url, json, headers, timeout)


MODELS_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "premium-coding", "object": "model"},
        {"id": "kr/claude-sonnet-4.5", "object": "model"},
        {"id": "kr/glm-5", "object": "model"},
        {"id": "oc/gpt-4o", "object": "model"},
    ],
}


def _models_fake(payload=None):
    payload = payload if payload is not None else MODELS_PAYLOAD

    def get(url, headers, timeout):
        assert url.endswith("/v1/models")
        return _response(payload)

    return FakeHttpx(get=get)


def _chat_payload(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# ------------------------------------------------------------ config


def test_9router_defaults_loopback_no_key(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text('[llm]\nprovider = "9router"\nmode = "local"\n')
    cfg = load_product_config(str(target))
    assert cfg.provider == "9router"
    assert cfg.style == "9router"
    assert cfg.mode == "local"
    assert cfg.model == "auto"
    assert cfg.base_url == "http://127.0.0.1:20128"
    assert cfg.api_key_env == "NINEROUTER_API_KEY"
    assert cfg.api_key == ""
    assert any("dashboard" in warning for warning in cfg.warnings)


def test_9router_remote_requires_key(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text(
        '[llm]\nprovider = "9router"\nmode = "remote"\n'
        'base_url = "https://router.example.com"\n'
    )
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "NINEROUTER_API_KEY" in str(exc.value)

    monkeypatch.setenv("NINEROUTER_API_KEY", "sk-test-123")
    cfg = load_product_config(str(target))
    assert cfg.api_key == "sk-test-123"


def test_invalid_provider_still_rejected(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text('[llm]\nprovider = "deepseek"\n')
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "invalid provider" in str(exc.value)
    assert "9router" in str(exc.value)


def test_write_llm_config_roundtrip(tmp_path, monkeypatch):
    target = str(tmp_path / "sub" / "config.toml")
    monkeypatch.setenv("NINEROUTER_API_KEY", "sk-secret-value")

    written = write_llm_config(
        target,
        provider="9router",
        model="premium-coding",
        base_url="http://127.0.0.1:20128",
        api_key_env="NINEROUTER_API_KEY",
    )
    assert written == target

    cfg = load_product_config(target)
    assert cfg.provider == "9router"
    assert cfg.model == "premium-coding"

    # Secrets never touch the file: only the variable name is stored.
    body = Path(target).read_text()
    assert "sk-secret-value" not in body
    assert "NINEROUTER_API_KEY" in body

    # Partial updates preserve the rest.
    write_llm_config(target, model="kr/glm-5")
    cfg = load_product_config(target)
    assert cfg.model == "kr/glm-5"
    assert cfg.provider == "9router"
    assert cfg.base_url == "http://127.0.0.1:20128"


def test_write_llm_config_preserves_other_sections(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        '[llm]\nprovider = "ollama"\n'
        '[server]\nprofile = "single-node"\nport = 9999\n'
        '[logging]\nlevel = "DEBUG"\n'
    )
    write_llm_config(str(target), provider="9router")
    cfg = load_product_config(str(target))
    assert cfg.provider == "9router"
    assert cfg.profile == "single-node"
    assert cfg.port == 9999
    assert cfg.log_level == "DEBUG"


def test_write_llm_config_rejects_bad_provider(tmp_path):
    with pytest.raises(ConfigError):
        write_llm_config(str(tmp_path / "c.toml"), provider="deepseek")


# ------------------------------------------------------------ ninerouter


def test_normalize_base_url():
    assert (
        ninerouter_module.normalize_base_url("http://x:20128/v1/")
        == "http://x:20128"
    )
    assert (
        ninerouter_module.normalize_base_url("http://x:20128/")
        == "http://x:20128"
    )
    assert ninerouter_module.normalize_base_url("") == (
        ninerouter_module.DEFAULT_BASE_URL
    )


def test_build_chat_request_shape():
    url, payload, headers = ninerouter_module.build_chat_request(
        "http://127.0.0.1:20128/",
        "premium-coding",
        "system",
        "user",
        api_key="sk-1",
    )
    assert url == "http://127.0.0.1:20128/v1/chat/completions"
    assert payload["model"] == "premium-coding"
    assert payload["stream"] is False
    assert payload["temperature"] == 0
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    assert headers["Authorization"] == "Bearer sk-1"

    _, _, bare = ninerouter_module.build_chat_request(
        "http://127.0.0.1:20128", "m", "s", "u"
    )
    assert "Authorization" not in bare


def test_parse_chat_response_ok_and_malformed():
    assert (
        ninerouter_module.parse_chat_response(_chat_payload("hi")) == "hi"
    )
    with pytest.raises(ninerouter_module.NinerouterError):
        ninerouter_module.parse_chat_response({"choices": []})
    with pytest.raises(ninerouter_module.NinerouterError):
        ninerouter_module.parse_chat_response({"nope": True})


def test_list_models_splits_combos(monkeypatch):
    fake = _models_fake()
    monkeypatch.setattr(ninerouter_module, "httpx", fake)
    listing = ninerouter_module.list_models("http://127.0.0.1:20128")
    assert listing["combos"] == ["premium-coding"]
    assert listing["models"] == [
        "kr/claude-sonnet-4.5",
        "kr/glm-5",
        "oc/gpt-4o",
    ]


def test_list_models_unreachable_is_actionable(monkeypatch):
    def get(url, headers, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        ninerouter_module, "httpx", FakeHttpx(get=get)
    )
    with pytest.raises(ninerouter_module.NinerouterError) as exc:
        ninerouter_module.list_models("http://127.0.0.1:20128")
    assert "`9router`" in str(exc.value)


def test_list_models_malformed(monkeypatch):
    fake = _models_fake(payload={"data": [{"nope": 1}]})
    monkeypatch.setattr(ninerouter_module, "httpx", fake)
    with pytest.raises(ninerouter_module.NinerouterError):
        ninerouter_module.list_models()


def test_pick_default_model_preferences():
    pick = ninerouter_module.pick_default_model
    assert pick(["b/m"], ["zzz", "premium-coding"]) == "premium-coding"
    assert pick(["b/m"], ["zzz", "aaa"]) == "aaa"
    assert pick(["kr/glm-5", "kr/claude-sonnet-4.5"], []) == (
        "kr/claude-sonnet-4.5"
    )
    assert pick(["zz/1", "aa/2"], []) == "aa/2"
    with pytest.raises(ninerouter_module.NinerouterError):
        pick([], [])


def test_resolve_model_explicit_skips_network(monkeypatch):
    def get(url, headers, timeout):
        raise AssertionError("must not hit the network")

    monkeypatch.setattr(
        ninerouter_module, "httpx", FakeHttpx(get=get)
    )
    assert (
        ninerouter_module.resolve_model(
            "http://127.0.0.1:20128", "", "kr/glm-5"
        )
        == "kr/glm-5"
    )


def test_resolve_model_auto_detects_and_caches(monkeypatch):
    fake = _models_fake()
    monkeypatch.setattr(ninerouter_module, "httpx", fake)
    first = ninerouter_module.resolve_model("http://127.0.0.1:20128")
    second = ninerouter_module.resolve_model("http://127.0.0.1:20128/")
    assert first == "premium-coding"
    assert second == "premium-coding"
    assert len(fake.get_calls) == 1


def test_probe_never_raises(monkeypatch):
    def get(url, headers, timeout):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(
        ninerouter_module, "httpx", FakeHttpx(get=get)
    )
    status = ninerouter_module.probe("http://127.0.0.1:9")
    assert status["ok"] is False
    assert "9Router" in status["error"]

    monkeypatch.setattr(
        ninerouter_module, "httpx", _models_fake()
    )
    status = ninerouter_module.probe("http://127.0.0.1:20128")
    assert status["ok"] is True
    assert status["default_model"] == "premium-coding"


def test_detect_install_shape(monkeypatch):
    monkeypatch.setattr(
        ninerouter_module, "httpx", _models_fake()
    )
    detection = ninerouter_module.detect_install(
        "http://127.0.0.1:20128", api_key="sk-1"
    )
    assert detection["reachable"] is True
    assert detection["api_key_set"] is True
    assert detection["cli_installed"] in (True, False)


# ------------------------------------------------------------ provider


def test_style_alias_ninerouter(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "ninerouter")
    provider = LocalLLMProvider()
    assert provider.style == "9router"
    assert provider.base_url == "http://127.0.0.1:20128"
    assert provider.model == "auto"


def test_unsupported_style_lists_9router(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "deepseek")
    provider = LocalLLMProvider()
    with pytest.raises(LLMError) as exc:
        provider.chat("s", "u")
    assert "9router" in str(exc.value)


def test_ninerouter_chat_payload(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")
    monkeypatch.setenv("NINEROUTER_API_KEY", "sk-test")

    def post(url, payload, headers, timeout):
        return _response(_chat_payload("hello"), method="POST", url=url)

    fake = FakeHttpx(post=post)
    monkeypatch.setattr(provider_module, "httpx", fake)

    provider = LocalLLMProvider()
    assert provider.chat("system", "user") == "hello"

    assert len(fake.post_calls) == 1
    call = fake.post_calls[0]
    assert call["url"] == "http://127.0.0.1:20128/v1/chat/completions"
    assert call["json"]["model"] == "premium-coding"
    assert call["json"]["stream"] is False
    assert call["headers"]["Authorization"] == "Bearer sk-test"


def test_ninerouter_chat_malformed(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")

    def post(url, payload, headers, timeout):
        return _response({"choices": []}, method="POST", url=url)

    monkeypatch.setattr(
        provider_module, "httpx", FakeHttpx(post=post)
    )
    with pytest.raises(LLMError):
        LocalLLMProvider().chat("s", "u")


def test_ninerouter_auto_model_resolves(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "auto")

    get_fake = _models_fake()
    monkeypatch.setattr(ninerouter_module, "httpx", get_fake)

    def post(url, payload, headers, timeout):
        assert payload["model"] == "premium-coding"
        return _response(_chat_payload("ok"), method="POST", url=url)

    monkeypatch.setattr(
        provider_module, "httpx", FakeHttpx(post=post)
    )
    assert LocalLLMProvider().chat("s", "u") == "ok"


def test_ninerouter_auto_no_models(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "auto")
    fake = _models_fake(payload={"data": []})
    monkeypatch.setattr(ninerouter_module, "httpx", fake)
    with pytest.raises(LLMError) as exc:
        LocalLLMProvider().chat("s", "u")
    assert "Connect a provider" in str(exc.value)


def _dead_9router_live_ollama(monkeypatch):
    """Primary 9Router refuses; ollama answers (fake transports)."""

    def post(url, payload, headers, timeout):
        if url.startswith("http://127.0.0.1:20128"):
            raise httpx.ConnectError("9router down")
        assert url == "http://127.0.0.1:11434/api/chat"
        return _response(
            {"message": {"content": "ollama-answer"}},
            method="POST",
            url=url,
        )

    fake = FakeHttpx(post=post)
    monkeypatch.setattr(provider_module, "httpx", fake)
    return fake


def test_fallback_chain_recovers(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")
    monkeypatch.setenv("YODAW_LLM_FALLBACKS", "ollama")
    fake = _dead_9router_live_ollama(monkeypatch)

    assert LocalLLMProvider().chat("s", "u") == "ollama-answer"

    attempts = pop_attempt_log()
    hops = [a for a in attempts if a.get("provider_fallback")]
    assert len(hops) == 1
    assert hops[0]["from"] == "9router"
    assert hops[0]["to"] == "ollama"
    assert any("20128" in call["url"] for call in fake.post_calls)
    assert any("11434" in call["url"] for call in fake.post_calls)


def test_sibling_uses_own_defaults(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv(
        "YODAW_LLM_BASE_URL", "http://127.0.0.1:20128"
    )
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")
    monkeypatch.setenv("YODAW_LLM_FALLBACKS", "ollama")
    fake = _dead_9router_live_ollama(monkeypatch)

    LocalLLMProvider().chat("s", "u")

    ollama_calls = [
        call for call in fake.post_calls if "11434" in call["url"]
    ]
    assert len(ollama_calls) == 1
    # The 9Router model/base must not leak into the ollama sibling.
    assert ollama_calls[0]["json"]["model"] == "qwen2.5-coder:7b"


def test_unknown_fallback_ignored(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")
    monkeypatch.setenv("YODAW_LLM_FALLBACKS", "deepseek,,ollama")
    assert fallback_styles() == ["ollama"]
    _dead_9router_live_ollama(monkeypatch)
    assert LocalLLMProvider().chat("s", "u") == "ollama-answer"


def test_no_fallback_single_attempt(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")

    def post(url, payload, headers, timeout):
        return _response(_chat_payload("one"), method="POST", url=url)

    fake = FakeHttpx(post=post)
    monkeypatch.setattr(provider_module, "httpx", fake)
    assert LocalLLMProvider().chat("s", "u") == "one"
    assert len(fake.post_calls) == 1
    assert fallback_styles() == []


def test_fallback_all_dead_surfaces_last_error(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")
    monkeypatch.setenv("YODAW_LLM_FALLBACKS", "ollama")

    def post(url, payload, headers, timeout):
        raise httpx.ConnectError("everything down")

    monkeypatch.setattr(
        provider_module, "httpx", FakeHttpx(post=post)
    )
    with pytest.raises(LLMError):
        LocalLLMProvider().chat("s", "u")
    hops = [
        a for a in pop_attempt_log() if a.get("provider_fallback")
    ]
    assert len(hops) == 1


def test_available_models_9router(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setattr(
        ninerouter_module, "httpx", _models_fake()
    )
    listing = LocalLLMProvider().available_models()
    assert listing["combos"] == ["premium-coding"]

    monkeypatch.setenv("YODAW_LLM_STYLE", "ollama")
    with pytest.raises(LLMError):
        LocalLLMProvider().available_models()


# ------------------------------------------------------------ SQLite repair


def test_repair_missing_creates_schema(tmp_path):
    from app.storage.db import check_database, repair_database

    target = tmp_path / "deep" / "nested" / "fresh.db"
    report = repair_database(target)
    assert report["ok"] is True
    assert report["repaired"] is True
    assert target.exists()

    status = check_database(target)
    assert status["ok"] is True
    assert "missions" in status["tables"]


def test_repair_healthy_is_noop(tmp_path):
    from app.storage.db import repair_database
    from app.storage.sqlite_store import MissionStore

    target = tmp_path / "healthy.db"
    MissionStore(target)
    report = repair_database(target)
    assert report["ok"] is True
    assert report["repaired"] is False


def test_repair_corrupt_backs_up_and_recreates(tmp_path):
    from app.storage.db import check_database, repair_database
    from app.storage.sqlite_store import MissionStore

    target = tmp_path / "corrupt.db"
    target.write_bytes(b"this is not a sqlite file at all" * 10)

    status = check_database(target)
    assert status["ok"] is False
    assert "db-repair" in status["error"]

    report = repair_database(target)
    assert report["ok"] is True
    assert report["repaired"] is True
    assert report["backup"] is not None
    backup = Path(report["backup"])
    assert backup.exists()
    assert backup.read_bytes().startswith(b"this is not")

    # Fresh schema is fully usable.
    store = MissionStore(target)
    assert store.status_counts() is not None
    assert check_database(target)["ok"] is True


def test_connect_wraps_errors_with_hint(tmp_path):
    import sqlite3

    from app.storage import db as db_module

    # A directory where the database file should be: sqlite opens it
    # but the first PRAGMA fails -> our actionable wrapper.
    target = tmp_path / "adir"
    target.mkdir()
    with pytest.raises(sqlite3.OperationalError) as exc:
        db_module.connect(target)
    assert "db-repair" in str(exc.value)


def test_ops_db_check_and_repair(tmp_path, capsys):
    from app.operations.cli import main as ops_main

    healthy = tmp_path / "ops.db"
    assert ops_main(["--db", str(healthy), "db-check"]) == 0

    corrupt = tmp_path / "ops-corrupt.db"
    corrupt.write_bytes(b"garbage" * 100)
    assert ops_main(["--db", str(corrupt), "db-check"]) == 1
    assert ops_main(["--db", str(corrupt), "db-repair"]) == 0
    assert ops_main(["--db", str(corrupt), "db-check"]) == 0
    capsys.readouterr()


# ------------------------------------------------------------ CLI


def test_config_set_creates_and_updates(tmp_path, capsys):
    from app.cli.main import main as cli_main

    target = str(tmp_path / "config.toml")
    code = cli_main(
        [
            "config", "set", "--path", target,
            "--provider", "9router", "--model", "premium-coding",
        ]
    )
    assert code == 0
    cfg = load_product_config(target)
    assert cfg.provider == "9router"
    assert cfg.model == "premium-coding"
    capsys.readouterr()


def test_config_set_requires_flag(tmp_path, capsys):
    from app.cli.main import main as cli_main

    assert cli_main(["config", "set"]) == 2
    capsys.readouterr()


def test_config_init_with_flags_upserts(tmp_path, capsys):
    from app.cli.main import main as cli_main

    target = str(tmp_path / "config.toml")
    assert cli_main(["config", "init", "--path", target]) == 0
    assert (
        cli_main(
            [
                "config", "init", "--path", target,
                "--provider", "9router",
            ]
        )
        == 0
    )
    assert load_product_config(target).provider == "9router"
    capsys.readouterr()


def _detection_ok(**overrides):
    detection = {
        "cli": "/usr/local/bin/9router",
        "cli_installed": True,
        "reachable": True,
        "api_key_set": False,
        "base_url": "http://127.0.0.1:20128",
        "probe": {
            "ok": True,
            "models": ["kr/claude-sonnet-4.5"],
            "combos": ["premium-coding"],
            "default_model": "premium-coding",
            "base_url": "http://127.0.0.1:20128",
        },
    }
    detection.update(overrides)
    return detection


def test_setup_9router_zero_touch(tmp_path, monkeypatch, capsys):
    from app.cli.main import main as cli_main

    monkeypatch.setattr(
        ninerouter_module,
        "detect_install",
        lambda base_url, api_key, timeout=10.0: _detection_ok(),
    )
    target = str(tmp_path / "config.toml")
    code = cli_main(
        ["setup-9router", "--path", target, "--no-verify"]
    )
    assert code == 0
    cfg = load_product_config(target)
    assert cfg.provider == "9router"
    assert cfg.model == "premium-coding"
    assert cfg.base_url == "http://127.0.0.1:20128"
    assert cfg.api_key_env == "NINEROUTER_API_KEY"
    out = capsys.readouterr().out
    assert "9Router is ready" in out


def test_setup_9router_json_and_verify(tmp_path, monkeypatch, capsys):
    from app.cli.main import main as cli_main

    monkeypatch.setattr(
        ninerouter_module,
        "detect_install",
        lambda base_url, api_key, timeout=10.0: _detection_ok(),
    )

    class FakeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def chat(self, system, user):
            assert self.kwargs["style"] == "9router"
            return "OK"

    monkeypatch.setattr(
        provider_module, "LocalLLMProvider", FakeProvider
    )
    target = str(tmp_path / "config.toml")
    code = cli_main(["setup-9router", "--path", target, "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["model"] == "premium-coding"
    assert payload["verified"] is True


def test_setup_9router_unreachable(monkeypatch, capsys):
    from app.cli.main import main as cli_main

    def fake_detect(base_url, api_key, timeout=10.0):
        return {
            "cli": None,
            "cli_installed": False,
            "reachable": False,
            "api_key_set": False,
            "base_url": "http://127.0.0.1:20128",
            "probe": {
                "ok": False,
                "error": "cannot reach 9Router at ...",
                "base_url": "http://127.0.0.1:20128",
            },
        }

    monkeypatch.setattr(
        ninerouter_module, "detect_install", fake_detect
    )
    assert cli_main(["setup-9router", "--no-verify"]) == 1
    err = capsys.readouterr().err
    assert "npm install -g 9router" in err


def test_models_lists_inventory(tmp_path, monkeypatch, capsys):
    from app.cli.main import main as cli_main

    target = tmp_path / "config.toml"
    target.write_text('[llm]\nprovider = "9router"\n')
    monkeypatch.setenv("YODAW_CONFIG", str(target))
    monkeypatch.setattr(
        ninerouter_module, "httpx", _models_fake()
    )
    assert cli_main(["models"]) == 0
    out = capsys.readouterr().out
    assert "premium-coding" in out
    assert "kr/claude-sonnet-4.5" in out

    assert cli_main(["models", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default_model"] == "premium-coding"


def test_models_wrong_provider(tmp_path, monkeypatch, capsys):
    from app.cli.main import main as cli_main

    target = tmp_path / "config.toml"
    target.write_text('[llm]\nprovider = "ollama"\n')
    monkeypatch.setenv("YODAW_CONFIG", str(target))
    assert cli_main(["models"]) == 2
    capsys.readouterr()


# ------------------------------------------------------------ hermetic E2E


EDIT_PLAN = {
    "action": "edit",
    "target_file": "app.py",
    "find": "return 'old'",
    "replace": "return 'hello'",
    "reason": "hermetic 9Router E2E",
}


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)

    def git(*args):
        subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
        )

    git("init")
    git("config", "user.email", "yodaw@test.local")
    git("config", "user.name", "YODAW Test")
    (repo / "app.py").write_text("def greet():\n    return 'old'\n")
    (repo / "test_app.py").write_text(
        "from app import greet\n\n"
        "def test_greet():\n"
        "    assert greet() == 'hello'\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    git("add", ".")
    git("commit", "-m", "baseline")


def _mock_9router(monkeypatch):
    """Route the 9Router provider at a fake (no network, stream=false)."""

    def post(url, payload, headers, timeout):
        assert url.endswith("/v1/chat/completions")
        assert payload["stream"] is False
        return _response(
            _chat_payload(json.dumps(EDIT_PLAN)),
            method="POST",
            url=url,
        )

    fake = FakeHttpx(post=post)
    monkeypatch.setattr(provider_module, "httpx", fake)
    return fake


def test_worker_e2e_through_mock_9router(tmp_path, monkeypatch):
    import app.workers.repo_code_worker as worker_module

    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")
    fake = _mock_9router(monkeypatch)

    repo = tmp_path / "repo"
    _init_repo(repo)

    worker = worker_module.RepoCodeWorker()
    result = worker.execute(
        f"Modify app.py to say def greet():\n    return 'hello'\n"
        f" ({uuid.uuid4().hex})",
        {"repo_path": str(repo)},
    )

    assert result["success"] is True
    # The worker commits on an isolated worktree branch: the commit
    # must be real and carry the edited content.
    import re

    commit = (result.get("output") or {}).get("commit_sha", "")
    assert re.fullmatch(r"[0-9a-f]{40}", commit or "")
    subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    committed = subprocess.run(
        ["git", "show", f"{commit}:app.py"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "return 'hello'" in committed
    assert fake.post_calls, "worker must call the 9Router endpoint"


def test_mission_e2e_through_mock_9router(tmp_path, monkeypatch):
    """Full public-API mission lifecycle over the (mocked) 9Router path."""
    import time

    from fastapi.testclient import TestClient

    import app.main as main_module

    monkeypatch.setenv("YODAW_LLM_STYLE", "9router")
    monkeypatch.setenv("YODAW_LLM_MODEL", "premium-coding")
    _mock_9router(monkeypatch)

    repo = tmp_path / "repo"
    _init_repo(repo)

    client = TestClient(main_module.app)
    response = client.post(
        "/api/v1/missions",
        json={
            "goal": (
                "Modify app.py to say def greet():\n"
                "    return 'hello'\n"
                f" ({uuid.uuid4().hex})"
            ),
            "capability": "code",
            "repo_path": str(repo),
        },
    )
    assert response.status_code == 200
    mission_id = response.json()["mission_id"]

    deadline = time.monotonic() + 120
    terminal = {}
    while time.monotonic() < deadline:
        terminal = client.get(
            f"/api/v1/missions/{mission_id}"
        ).json()
        if terminal["status"] in (
            "PASS",
            "FAIL",
            "BLOCKED",
            "BLOCKED_EXTERNAL",
            "CANCELLED",
        ):
            break
        time.sleep(0.2)

    assert terminal["status"] == "PASS", terminal.get("result")
    evidence = client.get(
        f"/api/v1/missions/{mission_id}/evidence"
    )
    assert evidence.status_code == 200
    assert len(evidence.json()) > 0
