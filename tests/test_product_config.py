"""
Worker B: product configuration layer.

Covers the canonical schema + loader: provider/model selection,
local vs remote mode, API-key configuration, validation, sane
defaults, environment overrides, redaction, and backwards
compatibility with the existing environment-driven runtime.

No live providers, no network, no real config files.
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import httpx
import pytest

from app.cli.main import EXIT_OK, EXIT_TASK_FAILURE, main as cli_main
from app.config import ConfigError, load_config
from app.llm import provider as provider_module
from app.llm.provider import LocalLLMProvider
from app.product_config import (
    LLM_API_KEY_ENV_VAR,
    LLM_BASE_URL_ENV,
    LLM_MODEL_ENV,
    LLM_STYLE_ENV,
    apply_product_config,
    config_file_candidates,
    load_product_config,
    redact_config,
    validate_product_config,
    write_default_config,
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
    "YODAW_PROFILE",
    "YODAW_HOST",
    "YODAW_PORT",
    "YODAW_LOG_LEVEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def hermetic_home(tmp_path, monkeypatch):
    """Isolate config discovery from the real user home and env."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def _write_config(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "config.toml"
    target.write_text(body)
    return target


def monkey_config_path(monkeypatch, tmp_path: Path) -> None:
    """Point YODAW_CONFIG at a missing file (explicit path misuse guard)."""
    monkeypatch.setenv("YODAW_CONFIG", str(tmp_path / "explicit-missing.toml"))


# ------------------------------------------------------------
# Valid config / sane defaults
# ------------------------------------------------------------

def test_defaults_with_no_config_file():
    cfg = load_product_config()
    assert cfg.provider == "ollama"
    assert cfg.mode == "local"
    assert cfg.model == "qwen2.5-coder:7b"
    assert cfg.base_url == "http://127.0.0.1:11434"
    assert cfg.api_key == ""
    assert cfg.profile == "local"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8844
    assert cfg.log_level == "INFO"


def test_valid_config_roundtrip(tmp_path, monkeypatch):
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"openai\"\nmode = \"remote\"\nmodel = \"gpt-4o-mini\"\n"
        "[server]\nprofile = \"single-node\"\n[logging]\nlevel = \"warning\"\n",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-valid-key-1234")
    cfg = load_product_config(str(target))
    assert cfg.provider == "openai"
    assert cfg.mode == "remote"
    assert cfg.style == "openai"
    assert cfg.model == "gpt-4o-mini"
    assert cfg.base_url == "https://api.openai.com"
    assert cfg.api_key == "sk-valid-key-1234"
    assert cfg.api_key_env == "OPENAI_API_KEY"
    assert cfg.profile == "single-node"
    assert cfg.log_level == "WARNING"
    assert validate_product_config(str(target)) == []


def test_write_default_config_roundtrip(tmp_path):
    path = write_default_config(str(tmp_path / "config.toml"))
    assert Path(path).is_file()
    cfg = load_product_config(path)
    assert cfg.provider == "ollama"
    assert cfg.model == "qwen2.5-coder:7b"


def test_write_default_config_refuses_overwrite(tmp_path):
    target = str(tmp_path / "config.toml")
    write_default_config(target)
    # A user-modified file is preserved unless --force.
    with open(target, "w") as handle:
        handle.write("[llm]\nprovider = \"openai\"\n")
    with pytest.raises(ConfigError):
        write_default_config(target)
    # force overwrites
    write_default_config(target, force=True)
    assert Path(target).read_text().startswith("# YODAW configuration")


# ------------------------------------------------------------
# Missing key / invalid provider / invalid model / invalid mode
# ------------------------------------------------------------

def test_missing_key_remote_rejected(tmp_path):
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"openai\"\nmode = \"remote\"\nmodel = \"gpt-4o-mini\"\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    message = str(exc.value)
    assert "API key" in message
    assert "OPENAI_API_KEY" in message


def test_missing_key_anthropic_rejected(tmp_path):
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"anthropic\"\nmode = \"remote\"\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def test_invalid_provider_rejected(tmp_path):
    target = _write_config(tmp_path, "[llm]\nprovider = \"deepseek\"\n")
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "invalid provider" in str(exc.value)
    assert "ollama" in str(exc.value) and "openai" in str(exc.value)


def test_invalid_mode_rejected(tmp_path):
    target = _write_config(tmp_path, "[llm]\nmode = \"sideways\"\n")
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "invalid mode" in str(exc.value)


def test_ollama_cannot_be_remote(tmp_path):
    target = _write_config(tmp_path, "[llm]\nprovider = \"ollama\"\nmode = \"remote\"\n")
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "local-only" in str(exc.value)


def test_invalid_model_rejected(tmp_path, monkeypatch):
    target = _write_config(tmp_path, "[llm]\nprovider = \"openai\"\nmodel = \"   \"\n")
    monkey_config_path(monkeypatch, tmp_path)
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "model" in str(exc.value)


def test_invalid_log_level_and_port(tmp_path, monkeypatch):
    monkey_config_path(monkeypatch, tmp_path)
    target = _write_config(
        tmp_path,
        "[logging]\nlevel = \"LOUD\"\n[server]\nport = 99999\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(target))
    assert "out of range" in str(exc.value)

    bad_level = _write_config(
        tmp_path,
        "[logging]\nlevel = \"LOUD\"\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_product_config(str(bad_level))
    assert "invalid log_level" in str(exc.value)


def test_config_validate_collects_errors(tmp_path):
    bad_provider = tmp_path / "provider.toml"
    bad_provider.write_text("[llm]\nprovider = \"deepseek\"\n")
    bad_mode = tmp_path / "mode.toml"
    bad_mode.write_text("[llm]\nmode = \"jump\"\n")
    assert any(
        "provider" in error
        for error in validate_product_config(str(bad_provider))
    )
    assert any(
        "mode" in error for error in validate_product_config(str(bad_mode))
    )


# ------------------------------------------------------------
# Local vs remote mode
# ------------------------------------------------------------

def test_local_mode_ollama_requires_no_key(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "ollama")
    monkeypatch.setenv("YODAW_LLM_MODE", "local")
    monkeypatch.setenv("YODAW_LLM_MODEL", "my-model")
    cfg = load_product_config()
    assert cfg.provider == "ollama"
    assert cfg.mode == "local"
    assert cfg.api_key == ""


def test_remote_mode_resolves_provider_and_key(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_MODE", "remote")
    monkeypatch.setenv("YODAW_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-remote-1")
    cfg = load_product_config()
    assert cfg.provider == "openai"  # auto -> openai in remote mode
    assert cfg.mode == "remote"
    assert cfg.api_key == "sk-remote-1"
    assert cfg.base_url == "https://api.openai.com"


def test_local_mode_openai_loopback_no_key(tmp_path, monkeypatch):
    monkey_config_path(monkeypatch, tmp_path)
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"openai\"\nmode = \"local\"\nmodel = \"gpt-4o-mini\"\n"
        "base_url = \"http://127.0.0.1:1234\"\n",
    )
    cfg = load_product_config(str(target))
    assert cfg.mode == "local"
    assert cfg.api_key == ""
    assert cfg.base_url == "http://127.0.0.1:1234"


def test_legacy_style_env_selects_provider(monkeypatch):
    monkeypatch.setenv(LLM_STYLE_ENV, "openai")
    monkeypatch.setenv(LLM_MODEL_ENV, "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    cfg = load_product_config()
    assert cfg.provider == "openai"


# ------------------------------------------------------------
# Env override (env always wins over file)
# ------------------------------------------------------------

def test_env_override_wins_over_file(tmp_path, monkeypatch):
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"ollama\"\nmodel = \"qwen2.5-coder:7b\"\n"
        "[server]\nport = 8844\n[logging]\nlevel = \"INFO\"\n",
    )
    monkeypatch.setenv("YODAW_LLM_PROVIDER", "openai")
    monkeypatch.setenv("YODAW_LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("YODAW_PORT", "9999")
    monkeypatch.setenv("YODAW_LOG_LEVEL", "debug")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    cfg = load_product_config(str(target))
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o"
    assert cfg.port == 9999
    assert cfg.log_level == "DEBUG"


def test_legacy_style_env_beats_file(tmp_path, monkeypatch):
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"openai\"\nmodel = \"gpt-4o-mini\"\n",
    )
    monkeypatch.setenv(LLM_STYLE_ENV, "ollama")
    monkeypatch.setenv(LLM_MODEL_ENV, "llama3.2")
    cfg = load_product_config(str(target))
    assert cfg.provider == "ollama"
    assert cfg.model == "llama3.2"


def test_explicit_config_env_missing_file_is_error(tmp_path, monkeypatch):
    monkeypatch.setenv("YODAW_CONFIG", str(tmp_path / "nope.toml"))
    with pytest.raises(ConfigError):
        load_product_config()


# ------------------------------------------------------------
# Redaction: no secrets in logs / output / errors
# ------------------------------------------------------------

def test_redacted_view_never_contains_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-abc123")
    monkeypatch.setenv("YODAW_LLM_STYLE", "openai")
    monkeypatch.setenv("YODAW_LLM_MODEL", "gpt-4o-mini")
    cfg = load_product_config()
    rendered = cfg.to_redacted_dict()
    assert rendered["api_key"] == "set"
    assert "sk-super-secret-abc123" not in str(rendered)
    for line in cfg.summary_lines():
        assert "sk-super-secret-abc123" not in line


def test_redact_config_deep_masks_secrets():
    payload = {
        "api_key": "sk-live-abcdefgh",
        "plain": "sk-live-abcdefgh leaked",
        "nested": {"Authorization": "Bearer sk-live-abcdefgh"},
        "model": "gpt-4o-mini",
    }
    out = redact_config(payload, secrets=("sk-live-abcdefgh",))
    assert out["api_key"] == "[REDACTED]"
    assert out["nested"]["Authorization"] == "[REDACTED]"
    assert "sk-live-abcdefgh" not in str(out)
    assert out["model"] == "gpt-4o-mini"


def test_config_cli_show_prints_no_secrets(monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-cli-secret-xyz")
    monkeypatch.setenv("YODAW_LLM_STYLE", "openai")
    monkeypatch.setenv("YODAW_LLM_MODEL", "gpt-4o-mini")
    assert cli_main(["config", "show"]) == EXIT_OK
    output = capsys.readouterr().out
    assert "sk-cli-secret-xyz" not in output
    assert "api_key   = set via OPENAI_API_KEY" in output


# ------------------------------------------------------------
# Backwards compatibility with the existing runtime
# ------------------------------------------------------------

def test_apply_is_noop_without_config_file():
    before = dict(os.environ)
    assert apply_product_config() == []
    assert dict(os.environ) == before


def test_apply_materializes_file_into_runtime_env(tmp_path, monkeypatch):
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"openai\"\nmode = \"remote\"\nmodel = \"gpt-4o-mini\"\n",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-apply-1")
    applied = apply_product_config(str(target))
    assert LLM_STYLE_ENV in applied
    assert LLM_MODEL_ENV in applied
    assert LLM_API_KEY_ENV_VAR in applied
    # The existing runtime reads these exact env vars.
    assert os.environ[LLM_STYLE_ENV] == "openai"
    assert os.environ[LLM_MODEL_ENV] == "gpt-4o-mini"
    assert os.environ[LLM_API_KEY_ENV_VAR] == "sk-apply-1"


def test_apply_never_overrides_existing_env(tmp_path, monkeypatch):
    # File is openai/auto; the legacy env overrides provider and
    # model, so the effective (valid) config is local ollama.
    target = _write_config(
        tmp_path,
        "[llm]\nprovider = \"openai\"\nmodel = \"gpt-4o-mini\"\n",
    )
    monkeypatch.setenv(LLM_STYLE_ENV, "ollama")
    monkeypatch.setenv(LLM_MODEL_ENV, "existing-model")
    apply_product_config(str(target))
    assert os.environ[LLM_STYLE_ENV] == "ollama"  # env wins
    assert os.environ[LLM_MODEL_ENV] == "existing-model"


def test_existing_runtime_load_config_unchanged(monkeypatch):
    monkeypatch.setenv("YODAW_PROFILE", "local")
    cfg = load_config()
    assert cfg.profile == "local"
    assert cfg.backend == "sqlite"


def test_legacy_provider_envs_still_drive_llm(monkeypatch):
    monkeypatch.setenv(LLM_STYLE_ENV, "ollama")
    monkeypatch.setenv(LLM_MODEL_ENV, "test-model")
    monkeypatch.setenv("YODAW_LLM_BASE_URL", "http://127.0.0.1:11434")
    provider = LocalLLMProvider()
    assert provider.style == "ollama"
    assert provider.model == "test-model"


def test_base_url_normalization_no_double_v1(monkeypatch):
    monkeypatch.setenv(LLM_STYLE_ENV, "openai")
    monkeypatch.setenv(LLM_MODEL_ENV, "gpt-4o-mini")
    monkeypatch.setenv(LLM_BASE_URL_ENV, "https://api.openai.com/v1")
    provider = LocalLLMProvider()
    assert provider.base_url == "https://api.openai.com"


# ------------------------------------------------------------
# Anthropic provider through the existing runtime
# ------------------------------------------------------------

def test_anthropic_config_selects_style(monkeypatch):
    monkeypatch.setenv("YODAW_LLM_STYLE", "anthropic")
    monkeypatch.setenv("YODAW_LLM_MODEL", "claude-sonnet-4-20250514")
    monkeypatch.setenv("YODAW_LLM_API_KEY", "sk-ant-api03-secret")
    cfg = load_product_config()
    assert cfg.provider == "anthropic"
    assert cfg.base_url == "https://api.anthropic.com"
    assert cfg.api_key == "sk-ant-api03-secret"


def _make_fake_httpx(handler, seen):
    fake = types.SimpleNamespace()
    fake.TimeoutException = httpx.TimeoutException
    fake.ConnectError = httpx.ConnectError
    fake.HTTPStatusError = httpx.HTTPStatusError

    def post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["payload"] = json
        request = httpx.Request("POST", url)
        return handler(request)

    fake.post = post
    return fake


def test_anthropic_chat_uses_messages_api(monkeypatch):
    seen = {}

    def handler(request):
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "the answer"}]},
            request=request,
        )

    monkeypatch.setattr(
        provider_module, "httpx", _make_fake_httpx(handler, seen)
    )
    monkeypatch.setenv(LLM_STYLE_ENV, "anthropic")
    monkeypatch.setenv(LLM_MODEL_ENV, "claude-sonnet-4-20250514")
    monkeypatch.setenv(LLM_API_KEY_ENV_VAR, "sk-ant-api03-secret")

    provider = LocalLLMProvider()
    result = provider.chat("system", "user")

    assert result == "the answer"
    assert seen["url"].endswith("/v1/messages")
    assert seen["headers"].get("x-api-key") == "sk-ant-api03-secret"
    assert seen["headers"].get("anthropic-version") == "2023-06-01"
    assert seen["payload"]["system"] == "system"


def test_anthropic_requires_key(monkeypatch):
    monkeypatch.setenv(LLM_STYLE_ENV, "anthropic")
    monkeypatch.setenv(LLM_MODEL_ENV, "claude-sonnet-4-20250514")
    monkeypatch.delenv(LLM_API_KEY_ENV_VAR, raising=False)
    provider = LocalLLMProvider()
    with pytest.raises(Exception) as exc:
        provider.chat("system", "user")
    assert "API key" in str(exc.value)


# ------------------------------------------------------------
# CLI first-run setup + validation commands
# ------------------------------------------------------------

def test_cli_config_init_validate_show(tmp_path, capsys, monkeypatch):
    target = str(tmp_path / "cli-config.toml")
    assert cli_main(["config", "init", "--path", target]) == EXIT_OK
    assert Path(target).is_file()
    assert cli_main(["config", "validate"]) == EXIT_OK

    # A broken file makes validate fail with exit 1.
    (tmp_path / "bad.toml").write_text("[llm]\nprovider = \"bogus\"\n")
    monkeypatch.setenv("YODAW_CONFIG", str(tmp_path / "bad.toml"))
    assert cli_main(["config", "validate"]) == EXIT_TASK_FAILURE
    assert "config error" in capsys.readouterr().err


def test_cli_config_commands_work_even_with_broken_file(tmp_path, capsys, monkeypatch):
    (tmp_path / "bad.toml").write_text("not [valid toml")
    monkeypatch.setenv("YODAW_CONFIG", str(tmp_path / "bad.toml"))
    # validate must report, not crash
    assert cli_main(["config", "validate"]) == EXIT_TASK_FAILURE
    # version must work regardless of config health
    assert cli_main(["version"]) == EXIT_OK


def test_config_file_candidates_mac_first(tmp_path):
    candidates = config_file_candidates()
    assert "Application Support" in candidates[0]
    assert candidates[0].endswith("config.toml")