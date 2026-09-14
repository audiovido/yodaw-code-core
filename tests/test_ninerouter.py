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

    def __init__(self, get=None, post=None, put=None, delete=None):
        self._get = get
        self._post = post
        self._put = put
        self._delete = delete
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.put_calls: list[dict] = []
        self.delete_calls: list[dict] = []

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

    def put(self, url, json=None, headers=None, timeout=None):
        self.put_calls.append(
            {"url": url, "json": json, "headers": dict(headers or {})}
        )
        assert self._put is not None, "unexpected PUT"
        return self._put(url, json, headers, timeout)

    def delete(self, url, headers=None, timeout=None):
        self.delete_calls.append(
            {"url": url, "headers": dict(headers or {})}
        )
        assert self._delete is not None, "unexpected DELETE"
        return self._delete(url, headers, timeout)


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


def _patch_zero_touch(
    monkeypatch, key="sk-local-zero-touch", local_servers=None
):
    """Patch every provisioning seam so the CLI flow stays hermetic."""
    monkeypatch.setattr(
        ninerouter_module,
        "detect_install",
        lambda base_url, api_key, timeout=10.0: _detection_ok(
            api_key_set=bool(api_key)
        ),
    )
    monkeypatch.setattr(
        ninerouter_module,
        "auto_provision",
        lambda *a, **k: {
            "base_url": "http://127.0.0.1:20128",
            "data_dir": str(Path.home() / ".9router"),
            "daemon": {"running": True, "started": False, "pid": None},
            "gateway_key": {
                "id": "key-1",
                "name": "yodaw-zero-touch",
                "status": "created",
                "key": key,
            },
            "local_servers": local_servers or [],
        },
    )


def test_setup_9router_zero_touch(tmp_path, monkeypatch, capsys):
    from app.cli.main import main as cli_main

    _patch_zero_touch(monkeypatch)
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
    # The LOCAL gateway key lives in a 0600 file, never the TOML.
    assert cfg.api_key == "sk-local-zero-touch"
    key_file = Path(cfg.api_key_file)
    assert key_file.is_file()
    import stat

    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert "sk-local-zero-touch" not in Path(target).read_text()
    out = capsys.readouterr().out
    assert "9Router is ready" in out
    assert "sk-local-zero-touch" not in out


def test_setup_9router_json_and_verify(tmp_path, monkeypatch, capsys):
    from app.cli.main import main as cli_main

    _patch_zero_touch(monkeypatch)

    class FakeProvider:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def chat(self, system, user):
            assert self.kwargs["style"] == "9router"
            assert self.kwargs["api_key"] == "sk-local-zero-touch"
            return "OK"

    monkeypatch.setattr(
        provider_module, "LocalLLMProvider", FakeProvider
    )
    target = str(tmp_path / "config.toml")
    code = cli_main(["setup-9router", "--path", target, "--json"])
    assert code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["model"] == "premium-coding"
    assert payload["verified"] is True
    assert payload["key_file"]
    # Raw key must never reach JSON output.
    assert "sk-local-zero-touch" not in out
    gk = payload["provisioning"]["gateway_key"]
    assert gk == {
        "id": "key-1",
        "name": "yodaw-zero-touch",
        "status": "created",
    }


def test_setup_9router_reuses_persisted_key_not_home_dir(
    tmp_path, monkeypatch, capsys
):
    """A re-run of setup-9router on a provisioned install must reuse
    the persisted LOCAL gateway key file (via product config) instead
    of attempting admin provisioning against ~/.9router."""
    from app.cli.main import main as cli_main

    # Product config from a previous provisioned install, plus its
    # owner-only local gateway key file.
    key = "sk-persisted-key"
    existing = tmp_path / "secrets" / "9router-api.key"
    existing.parent.mkdir(parents=True)
    existing.write_text(key + "\n")
    os.chmod(existing, 0o600)
    config = tmp_path / "product-config.toml"
    config.write_text(
        (
            "[llm]\n"
            'provider = "9router"\n'
            'mode = "local"\n'
            'model = "premium-coding"\n'
            'base_url = "http://127.0.0.1:20128"\n'
            'api_key_env = "NINEROUTER_API_KEY"\n'
            f'api_key_file = "{existing}"\n'
        )
    )
    target = str(tmp_path / "config.toml")
    monkeypatch.setenv("YODAW_CONFIG", str(config))
    # Drop any ambient key so the config file is the only source.
    monkeypatch.delenv("YODAW_LLM_API_KEY", raising=False)

    reached = {"admin_attempted": False}
    _patch_zero_touch(monkeypatch)

    def _fail_if_provisioned(*a, **k):
        # Reaching the admin provisioning path means the product's
        # persisted key was silently ignored (defaulting to the
        # ~/.9router data dir instead of the product's).
        reached["admin_attempted"] = True
        raise AssertionError(
            "setup-9router attempted admin provisioning despite a "
            "valid persisted LOCAL gateway key file"
        )

    monkeypatch.setattr(
        ninerouter_module, "auto_provision", _fail_if_provisioned
    )

    code = cli_main(["setup-9router", "--path", target, "--no-verify"])
    assert code == 0, capsys.readouterr().err
    assert reached["admin_attempted"] is False
    assert "9Router is ready" in capsys.readouterr().out


def test_setup_9router_empty_inventory_with_pin(tmp_path, monkeypatch, capsys):
    from app.cli.main import main as cli_main

    def fake_detect(base_url, api_key, timeout=10.0):
        return {
            "cli": "/usr/local/bin/9router",
            "cli_installed": True,
            "reachable": True,
            "api_key_set": True,
            "base_url": "http://127.0.0.1:20128",
            "probe": {
                "ok": True,
                "models": [],
                "combos": [],
                "default_model": None,
                "warning": "9Router reports no models and no combos.",
                "base_url": "http://127.0.0.1:20128",
            },
        }

    _patch_zero_touch(monkeypatch)
    monkeypatch.setattr(
        ninerouter_module, "detect_install", fake_detect
    )
    target = str(tmp_path / "config.toml")
    # No pin -> honest failure.
    assert cli_main(["setup-9router", "--path", target, "--no-verify"]) == 1
    capsys.readouterr()
    # Explicit pin bypasses the empty inventory (ollama-local quirk).
    assert (
        cli_main(
            [
                "setup-9router", "--path", target, "--no-verify",
                "--model", "ollama-local/qwen2.5-coder:0.5b",
            ]
        )
        == 0
    )
    cfg = load_product_config(target)
    assert cfg.model == "ollama-local/qwen2.5-coder:0.5b"
    capsys.readouterr()


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

    def fake_auto_provision(*a, **k):
        raise ninerouter_module.NinerouterAdminError(
            "9Router CLI not found; install with "
            "`npm install -g 9router`"
        )

    monkeypatch.setattr(
        ninerouter_module, "auto_provision", fake_auto_provision
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


# ============================================================
# Zero-touch local provisioning (admin API, key lifecycle,
# idempotent nodes/connections, full orchestration)
# ============================================================

def test_cli_secret_created_owner_only(tmp_path):
    data = tmp_path / "9r"
    first = ninerouter_module.ensure_cli_secret(data)
    import stat

    secret = data / "auth" / "cli-secret"
    assert secret.is_file()
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE((data / "auth").stat().st_mode) == 0o700
    # Idempotent: same secret, no churn.
    assert ninerouter_module.ensure_cli_secret(data) == first


def test_derive_cli_token(tmp_path, monkeypatch):
    nr = ninerouter_module
    data = tmp_path / "9r"
    # No OS machine id derivable at all -> empty token, never a guess.
    monkeypatch.setattr(nr, "system_machine_id", lambda: "")
    assert nr.derive_cli_token(data) == ""
    (data).mkdir(parents=True, exist_ok=True)
    (data / "machine-id").write_text("machine-123\n")
    ninerouter_module.ensure_cli_secret(data)
    token = ninerouter_module.derive_cli_token(data)
    import hashlib

    secret = (data / "auth" / "cli-secret").read_text().strip()
    expected = hashlib.sha256(
        ("machine-123" + "9r-cli-auth" + secret).encode()
    ).hexdigest()[:16]
    assert token == expected


def test_derive_token_without_persisted_machine_id(
    tmp_path, monkeypatch
):
    nr = ninerouter_module
    monkeypatch.setattr(nr, "system_machine_id", lambda: "sysid-hash")
    data = tmp_path / "9r"  # no machine-id file, like a fresh daemon
    token = nr.derive_cli_token(data)
    import hashlib

    secret = (data / "auth" / "cli-secret").read_text().strip()
    expected = hashlib.sha256(
        ("sysid-hash" + "9r-cli-auth" + secret).encode()
    ).hexdigest()[:16]
    assert token == expected
    # Persisted file wins once the daemon has written it.
    data.mkdir(parents=True, exist_ok=True)
    (data / "machine-id").write_text("file-id")
    token2 = nr.derive_cli_token(data)
    expected2 = hashlib.sha256(
        ("file-id" + "9r-cli-auth" + secret).encode()
    ).hexdigest()[:16]
    assert token2 == expected2 and token2 != token


def test_admin_request_dispatch_and_errors(monkeypatch):
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw.get("json")))
        if url.endswith("/api/explode"):
            return _response({"error": "boom"}, status=400, method=method, url=url)
        return _response({}, method=method, url=url)

    fake_http = FakeHttpx(
        get=lambda u, h, t: fake("GET", u),
        post=lambda u, j, h, t: fake("POST", u, json=j),
        delete=lambda u, h, t: fake("DELETE", u),
    )
    monkeypatch.setattr(ninerouter_module, "httpx", fake_http)
    nr = ninerouter_module
    nr._admin_request("POST", "/api/keys", nr.DEFAULT_BASE_URL, "tok", body={"name": "x"})
    nr._admin_request("DELETE", "/api/keys/k1", nr.DEFAULT_BASE_URL, "tok")
    with pytest.raises(nr.NinerouterAdminError, match="boom"):
        nr._admin_request("GET", "/api/explode", nr.DEFAULT_BASE_URL, "tok")
    assert calls[0][0] == "POST" and calls[0][2] == {"name": "x"}
    assert calls[1][0] == "DELETE"
    # Admin token header present, never a Bearer secret.
    req_headers = fake_http.post_calls[0]["headers"]
    assert req_headers["x-9r-cli-token"] == "tok"
    assert "Authorization" not in req_headers


def _admin_fake_httpx(state):
    """Fake admin API backed by an in-memory state dict."""

    def get(url, headers, timeout):
        if url.endswith("/v1/models"):
            return _response(
                {"data": [{"id": "local/smollm2-135m"}]}
            )
        if url.endswith("/api/keys"):
            return _response({"keys": state["keys"]})
        if url.endswith("/api/provider-nodes"):
            return _response({"nodes": state["nodes"]})
        if url.endswith("/api/providers"):
            return _response({"connections": state["connections"]})
        if url.endswith("/api/health"):
            return _response({"ok": True})
        raise AssertionError(f"unexpected GET {url}")

    def post(url, body, headers, timeout):
        if url.endswith("/v1/chat/completions"):
            auth = headers.get("Authorization")
            if auth == f"Bearer {state['valid_key']}":
                return _response(
                    {"choices": [{"message": {"content": "ok"}}]},
                    method="POST", url=url,
                )
            return _response(
                {"error": "invalid api key"}, status=401,
                method="POST", url=url,
            )
        if url.endswith("/api/keys"):
            record = {
                "id": f"key-{len(state['keys'])}",
                "key": state["next_key"],
                "name": body["name"],
                "isActive": True,
            }
            state["next_key"] += "x"
            state["keys"].append(record)
            return _response(record, status=201, method="POST", url=url)
        if url.endswith("/api/provider-nodes"):
            node = {"id": f"node-{len(state['nodes'])}", **body}
            state["nodes"].append(node)
            return _response({"node": node}, status=201, method="POST", url=url)
        if url.endswith("/api/providers"):
            conn = {
                "id": f"conn-{len(state['connections'])}",
                "provider": body["provider"],
                "name": body["name"],
            }
            state["connections"].append(conn)
            return _response(
                {"connection": conn}, status=201, method="POST", url=url
            )
        raise AssertionError(f"unexpected POST {url}")

    def delete(url, headers, timeout):
        if "/api/keys/" in url:
            key_id = url.rsplit("/", 1)[-1]
            state["keys"] = [k for k in state["keys"] if k["id"] != key_id]
        return _response({"message": "deleted"}, method="DELETE", url=url)

    return FakeHttpx(get=get, post=post, delete=delete)


def _admin_state(**overrides):
    state = {
        "keys": [],
        "nodes": [],
        "connections": [],
        "next_key": "sk-fresh-1",
        "valid_key": overrides.pop("valid_key", None),
    }
    state.update(overrides)
    return state


def test_provision_gateway_key_creates_then_reuses(monkeypatch):
    nr = ninerouter_module
    state = _admin_state(valid_key=None)
    monkeypatch.setattr(nr, "httpx", _admin_fake_httpx(state))

    first = nr.provision_gateway_key(token="t")
    assert first["status"] == "created" and first["key"] == "sk-fresh-1"
    # Second run with no key handed back: named key found + valid now.
    state["valid_key"] = "sk-fresh-1"
    second = nr.provision_gateway_key(token="t")
    assert second["status"] == "reused" and second["key"] == "sk-fresh-1"
    assert len(state["keys"]) == 1  # no churn
    # Valid key supplied explicitly: no admin traffic at all.
    fake = nr.httpx
    calls_before = len(fake.get_calls)
    third = nr.provision_gateway_key(token="t", existing_key="sk-fresh-1")
    assert third["status"] == "reused"
    assert len(fake.get_calls) == calls_before + 1  # only /v1/models validate


def test_provision_gateway_key_rotates_only_when_rejected(monkeypatch):
    nr = ninerouter_module
    state = _admin_state()
    state["valid_key"] = "sk-new-1"
    state["next_key"] = "sk-new-1"
    state["keys"] = [
        {
            "id": "key-dead",
            "key": "sk-dead",
            "name": nr.DEFAULT_KEY_NAME,
            "isActive": True,
        }
    ]
    fake = _admin_fake_httpx(state)
    monkeypatch.setattr(nr, "httpx", fake)

    result = nr.provision_gateway_key(token="t")
    # Dead named key is rejected (401) -> deleted + replaced once.
    assert result["status"] == "rotated"
    assert result["key"] == "sk-new-1"
    assert [k["id"] for k in state["keys"]] == ["key-0"]
    assert any("/api/keys/key-dead" in c["url"] for c in fake.delete_calls)


def test_validate_gateway_key_401_only_failure(monkeypatch):
    nr = ninerouter_module
    assert nr.validate_gateway_key("") is False

    def http_401(url, headers=None, timeout=None):
        if url.endswith("/v1/models"):
            return _response(
                {"data": [{"id": "m/one"}]}, url=url
            )
        return _response(
            {"error": {"message": "invalid"}}, status=401,
            method="POST", url=url,
        )

    def http_200(url, headers=None, timeout=None):
        if url.endswith("/v1/models"):
            return _response({"data": [{"id": "m/one"}]}, url=url)
        return _response(
            {"choices": [{"message": {"content": "ok"}}]},
            method="POST", url=url,
        )

    monkeypatch.setattr(nr, "httpx", FakeHttpx(get=http_401, post=lambda u, j, h, t: http_401(u, h, t)))
    assert nr.validate_gateway_key("sk-x") is False
    monkeypatch.setattr(nr, "httpx", FakeHttpx(get=http_200, post=lambda u, j, h, t: http_200(u, h, t)))
    assert nr.validate_gateway_key("sk-x") is True
    # Empty inventory -> inconclusive -> valid (never rotate).
    monkeypatch.setattr(
        nr,
        "httpx",
        FakeHttpx(get=lambda u, h, t: _response({"data": []}, url=u)),
    )
    assert nr.validate_gateway_key("sk-x") is True
    # Transport failure must NOT trigger rotation: treated valid.
    def boom(*a, **k):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(
        nr,
        "httpx",
        FakeHttpx(get=lambda u, h, t: _response({"data": [{"id": "m/one"}]}, url=u), post=boom),
    )
    assert nr.validate_gateway_key("sk-x") is True


def test_ensure_provider_node_idempotent_and_conflict(monkeypatch):
    nr = ninerouter_module
    state = _admin_state()
    fake = _admin_fake_httpx(state)
    monkeypatch.setattr(nr, "httpx", fake)

    first = nr.ensure_provider_node(
        name="Local", prefix="gem", node_base_url="http://127.0.0.1:8090/v1"
    )
    assert first["created"] is True
    # Same prefix + url -> idempotent no-op.
    second = nr.ensure_provider_node(
        name="Local", prefix="gem", node_base_url="http://127.0.0.1:8090/v1"
    )
    assert second["created"] is False
    assert second["node"]["id"] == first["node"]["id"]
    assert len(state["nodes"]) == 1
    # Same prefix, different url -> honest conflict error (never
    # silently shadows an existing node).
    with pytest.raises(nr.NinerouterAdminError, match="already exists"):
        nr.ensure_provider_node(
            name="Local2", prefix="gem", node_base_url="http://127.0.0.1:9999/v1"
        )


def test_ensure_provider_connection_idempotent(monkeypatch):
    nr = ninerouter_module
    state = _admin_state()
    monkeypatch.setattr(nr, "httpx", _admin_fake_httpx(state))

    node = nr.ensure_provider_node(
        name="Local", prefix="gem", node_base_url="http://127.0.0.1:8090/v1"
    )["node"]
    a = nr.ensure_provider_connection(
        node_id=node["id"], connection_name="gem-local"
    )
    b = nr.ensure_provider_connection(
        node_id=node["id"], connection_name="gem-local"
    )
    assert a["created"] is True and b["created"] is False
    assert a["connection"]["id"] == b["connection"]["id"]
    assert len(state["connections"]) == 1
    # Placeholder credential used for local no-auth servers, never a
    # real-looking secret.
    body = nr.httpx.post_calls[1]["json"]
    assert body["apiKey"] == nr.LOCAL_PLACEHOLDER_CREDENTIAL


def test_placeholder_connection_dedupes_by_node(monkeypatch):
    # A pre-existing connection under a DIFFERENT name (e.g. one
    # created through the dashboard) must still satisfy a later
    # zero-touch run for a local, no-auth node.
    nr = ninerouter_module
    state = _admin_state()
    state["nodes"] = [{"id": "node-0", "prefix": "gem"}]
    state["connections"] = [
        {"id": "conn-old", "provider": "node-0", "name": "manual-name",
         "isActive": True}
    ]
    monkeypatch.setattr(nr, "httpx", _admin_fake_httpx(state))
    out = nr.ensure_provider_connection(
        node_id="node-0", connection_name="gem-local"
    )
    assert out["created"] is False
    assert out["connection"]["id"] == "conn-old"


def test_parse_local_server_spec():
    nr = ninerouter_module
    parsed = nr.parse_local_server_spec(
        "Local llama (Gemma):gemma:http://127.0.0.1:8090/v1"
    )
    assert parsed == {
        "name": "Local llama (Gemma)",
        "prefix": "gemma",
        "base_url": "http://127.0.0.1:8090/v1",
    }
    for bad in ("", "garbage", "n:p", "n:p:ftp://x"):
        with pytest.raises(nr.NinerouterAdminError):
            nr.parse_local_server_spec(bad)


def test_auto_provision_full_orchestration(tmp_path, monkeypatch):
    nr = ninerouter_module
    state = _admin_state()
    monkeypatch.setattr(nr, "httpx", _admin_fake_httpx(state))
    monkeypatch.setattr(nr, "daemon_health", lambda *a, **k: True)
    monkeypatch.setattr(nr, "resolve_admin", lambda b, d=None, **k: (d, "tok"))

    data = tmp_path / "9r"
    result = nr.auto_provision(
        data_dir=data,
        existing_key="",
        local_servers=[
            {"name": "Gemma", "prefix": "gemma",
             "base_url": "http://127.0.0.1:8090/v1"}
        ],
    )
    assert result["gateway_key"]["status"] == "created"
    assert result["gateway_key"]["key"] == "sk-fresh-1"
    assert len(result["local_servers"]) == 1
    server = result["local_servers"][0]
    assert server["prefix"] == "gemma"
    assert server["node_created"] is True
    assert server["connection_created"] is True
    # Attaching to an already-healthy daemon must not litter the
    # requested data dir with a secret it will never use.
    assert not (data / "auth" / "cli-secret").exists()

    # Second run: everything idempotent, no fresh key.
    state["valid_key"] = "sk-fresh-1"
    again = nr.auto_provision(
        data_dir=data,
        existing_key="",
        local_servers=[
            {"name": "Gemma", "prefix": "gemma",
             "base_url": "http://127.0.0.1:8090/v1"}
        ],
    )
    assert again["gateway_key"]["status"] == "reused"
    assert again["local_servers"][0]["node_created"] is False
    assert again["local_servers"][0]["connection_created"] is False
    assert len(state["keys"]) == 1
    assert len(state["nodes"]) == 1
    assert len(state["connections"]) == 1


def test_auto_provision_starts_daemon(tmp_path, monkeypatch):
    nr = ninerouter_module
    state = _admin_state()
    monkeypatch.setattr(nr, "httpx", _admin_fake_httpx(state))
    monkeypatch.setattr(nr, "find_cli", lambda: "/usr/local/bin/9router")
    monkeypatch.setattr(nr, "resolve_admin", lambda b, d=None, **k: (d, "tok"))
    started = {"n": 0}

    health = {"up": False}

    def fake_health(*a, **k):
        return health["up"]

    def fake_start(data_dir=None, port=0, log_file=None):
        started["n"] += 1
        health["up"] = True
        return 4242

    monkeypatch.setattr(nr, "daemon_health", fake_health)
    monkeypatch.setattr(nr, "start_daemon", fake_start)

    result = nr.auto_provision(data_dir=tmp_path / "9r", install=False)
    assert started["n"] == 1
    assert result["daemon"]["started"] is True
    assert result["daemon"]["pid"] == 4242


def test_setup_9router_registers_local_servers(
    tmp_path, monkeypatch, capsys
):
    from app.cli.main import main as cli_main

    captured = {}

    def fake_auto_provision(base_url, **kwargs):
        captured["kwargs"] = kwargs
        return {
            "base_url": base_url,
            "data_dir": kwargs["data_dir"],
            "daemon": {"running": True, "started": False, "pid": None},
            "gateway_key": {
                "id": "key-1",
                "name": "yodaw-zero-touch",
                "status": "created",
                "key": "sk-local-1",
            },
            "local_servers": [
                {"prefix": "gemma", "base_url": "http://127.0.0.1:8090/v1",
                 "node_created": True, "connection_created": True,
                 "node_id": "n1", "connection_id": "c1", "name": "Gemma"}
            ],
        }

    monkeypatch.setattr(
        ninerouter_module, "auto_provision", fake_auto_provision
    )
    monkeypatch.setattr(
        ninerouter_module,
        "detect_install",
        lambda base_url, api_key, timeout=10.0: _detection_ok(
            models=["gemma/gemma3-270m"], combos=[],
            probe={
                "ok": True,
                "models": ["gemma/gemma3-270m"],
                "combos": [],
                "default_model": "gemma/gemma3-270m",
                "base_url": "http://127.0.0.1:20128",
            },
            api_key_set=bool(api_key),
        ),
    )

    target = str(tmp_path / "config.toml")
    code = cli_main(
        [
            "setup-9router", "--path", target, "--no-verify",
            "--register-local",
            "Gemma:gemma:http://127.0.0.1:8090/v1",
        ]
    )
    assert code == 0
    servers = captured["kwargs"]["local_servers"]
    assert servers == [
        {"name": "Gemma", "prefix": "gemma",
         "base_url": "http://127.0.0.1:8090/v1"}
    ]
    out = capsys.readouterr().out
    assert "gemma -> http://127.0.0.1:8090/v1" in out
    assert "sk-local-1" not in out
    cfg = load_product_config(target)
    assert cfg.model == "gemma/gemma3-270m"


def test_key_file_config_roundtrip_and_perms(tmp_path, monkeypatch):
    from app.product_config import (
        ConfigError,
        write_api_key_file,
        write_llm_config,
    )

    target = str(tmp_path / "config.toml")
    key_file = write_api_key_file("sk-loopback", str(tmp_path / "k.key"))
    write_llm_config(
        target,
        provider="9router",
        model="auto",
        base_url="http://127.0.0.1:20128",
        api_key_env="NINEROUTER_API_KEY",
        api_key_file=key_file,
    )
    cfg = load_product_config(target)
    assert cfg.api_key == "sk-loopback"
    assert cfg.api_key_file == key_file

    # A loosened key file is detected and re-tightened.
    os.chmod(key_file, 0o644)
    cfg2 = load_product_config(target)
    import stat

    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    assert any("0600" in w for w in cfg2.warnings)

    # Missing referenced key file with no env key -> hard error.
    os.unlink(key_file)
    with pytest.raises(ConfigError, match="does not exist"):
        load_product_config(target)

    # Environment wins over the file; a missing file degrades to a
    # warning when the env supplies the key.
    monkeypatch.setenv("NINEROUTER_API_KEY", "sk-from-env")
    cfg3 = load_product_config(target)
    assert cfg3.api_key == "sk-from-env"
    assert any("does not exist" in w for w in cfg3.warnings)


def test_lenient_json_loads_tolerates_sse_trailer():
    nr = ninerouter_module
    body = json.dumps(_chat_payload("OK"))
    tolerant = nr.lenient_json_loads(body + '\ndata: [DONE]\n\n')
    assert nr.parse_chat_response(tolerant) == "OK"
    # Plain JSON still works; garbage and non-objects fail clearly.
    assert nr.lenient_json_loads(body)["choices"]
    with pytest.raises(json.JSONDecodeError):
        nr.lenient_json_loads("not json at all")
    with pytest.raises(nr.NinerouterError):
        nr.lenient_json_loads("[1,2,3]")


def test_provider_tolerates_sse_trailer_on_nonstream(monkeypatch):
    nr = ninerouter_module
    body = json.dumps(_chat_payload("hi")) + 'data: [DONE]\n\n'

    def post(url, json=None, headers=None, timeout=None):
        return _response(
            None, status=200, method="POST", url=url
        ).__class__(200, content=body.encode(),
                     request=httpx.Request("POST", url))

    fake = FakeHttpx(post=post)
    monkeypatch.setattr(provider_module.httpx, "post", fake.post)
    provider = provider_module.LocalLLMProvider(
        style="9router",
        base_url="http://127.0.0.1:20128",
        model="local/smollm2-135m",
        api_key="sk-x",
    )
    assert provider.chat("s", "u") == "hi"


def test_resolve_admin_attaches_to_running_daemon_dir(
    tmp_path, monkeypatch
):
    nr = ninerouter_module
    requested = tmp_path / "requested"
    running = tmp_path / "running-data"
    running.mkdir()
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr(
        nr, "discover_running_data_dir", lambda port=None: running
    )
    monkeypatch.setattr(nr, "default_data_dir", lambda: tmp_path / "default")

    def fake_try(base_url, data_dir, timeout=10.0):
        return "tok" if data_dir == running else None

    monkeypatch.setattr(nr, "_try_admin_token", fake_try)
    resolved, token = nr.resolve_admin(
        "http://127.0.0.1:20128", requested
    )
    assert resolved == running and token == "tok"

    # Requested dir authenticating always wins, never hijacked.
    monkeypatch.setattr(
        nr, "_try_admin_token",
        lambda b, d, timeout=10.0: "own" if d == requested else None,
    )
    resolved, token = nr.resolve_admin(
        "http://127.0.0.1:20128", requested
    )
    assert resolved == requested and token == "own"


def test_resolve_admin_no_match_lists_attempts(tmp_path, monkeypatch):
    nr = ninerouter_module
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setattr(nr, "discover_running_data_dir", lambda port=None: None)
    monkeypatch.setattr(nr, "default_data_dir", lambda: tmp_path / "default")
    monkeypatch.setattr(nr, "_try_admin_token", lambda *a, **k: None)
    with pytest.raises(nr.NinerouterAdminError, match="tried:"):
        nr.resolve_admin("http://127.0.0.1:20128", tmp_path / "req")


def test_start_daemon_precreates_secret_and_binds_loopback(
    tmp_path, monkeypatch
):
    nr = ninerouter_module
    monkeypatch.setattr(nr, "find_cli", lambda: "/usr/local/bin/9router")
    captured = {}

    class FakeProc:
        pid = 9911

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        return FakeProc()

    monkeypatch.setattr(nr.subprocess, "Popen", fake_popen)
    data = tmp_path / "9r"
    pid = nr.start_daemon(data_dir=data, port=20131)
    assert pid == 9911
    assert "--host" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--host") + 1] == "127.0.0.1"
    assert captured["env"]["DATA_DIR"] == str(data)
    import stat

    secret = data / "auth" / "cli-secret"
    assert secret.is_file()
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
