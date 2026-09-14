"""
Production configuration layer (Worker B).

One canonical schema, one TOML config file, no manual environment
editing. The loader resolves file + environment (environment
wins), validates everything up front, and bridges the result into
the environment variables the existing runtime already reads, so
the runtime itself is untouched.

Canonical file locations (first match wins):

    YODAW_CONFIG
    ~/Library/Application Support/yodaw/config.toml   (macOS)
    ${XDG_CONFIG_HOME:-~/.config}/yodaw/config.toml
    ./yodaw.config.toml

Environment always wins over the file. Upstream provider secrets
live in environment variables (never the config file and never
logs); the file only names the variable via `api_key_env`.

The sole on-disk secret is the LOCAL 9Router gateway key, which
zero-touch provisioning stores in a separate owner-only (0600)
file referenced by `api_key_file`. It authenticates the local
daemon only - it is never an upstream provider credential.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from app.config import ConfigError, PROFILES

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.9 fallback
    tomllib = None

REDACTED = "[REDACTED]"

PROVIDERS = ("auto", "ollama", "openai", "anthropic", "9router")
MODES = ("auto", "local", "remote")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

MODEL_DEFAULTS = {
    "ollama": "qwen2.5-coder:7b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
    # "auto" for 9Router means: detect at runtime via GET
    # /v1/models (combos preferred). It is a valid persisted value.
    "9router": "auto",
}

BASE_URL_DEFAULTS = {
    "ollama": "http://127.0.0.1:11434",
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "9router": "http://127.0.0.1:20128",
}

API_KEY_ENV_DEFAULTS = {
    "ollama": "",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "9router": "NINEROUTER_API_KEY",
}

# Environment variables this layer reads (legacy and new).
LLM_PROVIDER_ENV = "YODAW_LLM_PROVIDER"   # new
LLM_STYLE_ENV = "YODAW_LLM_STYLE"         # legacy, ollama|openai
LLM_MODE_ENV = "YODAW_LLM_MODE"           # new
LLM_MODEL_ENV = "YODAW_LLM_MODEL"
LLM_BASE_URL_ENV = "YODAW_LLM_BASE_URL"
LLM_API_KEY_ENV_VAR = "YODAW_LLM_API_KEY"      # literal key (legacy)
LLM_API_KEY_ENV_NAME_ENV = "YODAW_LLM_API_KEY_ENV"
LLM_API_KEY_FILE_ENV = "YODAW_LLM_API_KEY_FILE"  # owner-only key file

_KEY_FILE_MODE = 0o600
_KEY_DIR_MODE = 0o700

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

_CONFIG_TEMPLATE_NAME = "config.toml"


def _harden_key_file_permissions(path: str) -> Optional[str]:
    """Tighten a key file/dir to owner-only; return a warning or None."""
    warning = None
    try:
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            os.chmod(path, _KEY_FILE_MODE)
            warning = (
                f"key file {path} was group/world-readable; "
                "permissions were tightened to 0600"
            )
    except OSError:
        warning = (
            f"could not verify/tighten permissions on key file {path}; "
            "ensure it is owner-only readable (chmod 600)"
        )
    return warning


def _resolve_key_file(value: str, source_path: Optional[str]) -> Optional[str]:
    """Resolve an api_key_file path; relative paths anchor at config."""
    value = (value or "").strip()
    if not value:
        return None
    if os.path.isabs(value):
        return value
    if source_path:
        return os.path.join(os.path.dirname(os.path.abspath(source_path)), value)
    return os.path.abspath(value)


def _read_key_file(path: str) -> str:
    """Read a trimmed secret from an owner-only file."""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def default_api_key_file(config_path: Optional[str] = None) -> str:
    """Default owner-only key file alongside the product config."""
    target = config_path or config_file_candidates()[1]
    return os.path.join(os.path.dirname(os.path.abspath(target)), "secrets", "9router-api.key")


def write_api_key_file(key: str, path: Optional[str] = None) -> str:
    """Persist a LOCAL gateway key to a 0600 file inside a 0700 dir.

    Returns the path. The secret is written without ever touching
    logs, the TOML config, or stdout. Creation is atomic and the
    file is created owner-only even on the first write.
    """
    target = path or default_api_key_file()
    directory = os.path.dirname(os.path.abspath(target))
    os.makedirs(directory, mode=_KEY_DIR_MODE, exist_ok=True)
    try:
        os.chmod(directory, _KEY_DIR_MODE)
    except OSError:
        pass
    temp_path = f"{target}.tmp-{os.getpid()}"
    descriptor = os.open(
        temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KEY_FILE_MODE
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(key.strip() + "\n")
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    os.replace(temp_path, target)
    os.chmod(target, _KEY_FILE_MODE)
    return target


def _is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


def _load_toml(path: str) -> dict:
    global tomllib
    if tomllib is None:
        try:
            import tomli as tomllib  # type: ignore
        except ModuleNotFoundError:
            raise ConfigError(
                "the YODAW config file is TOML; Python 3.11+ "
                "(tomllib) or the 'tomli' package is required to "
                "read it"
            )
    with open(path, "rb") as handle:
        try:
            return tomllib.load(handle)
        except Exception as exc:
            raise ConfigError(
                f"invalid config file {path}: {exc}"
            ) from exc


# ------------------------------------------------------------
# Canonical schema
# ------------------------------------------------------------

@dataclass(frozen=True)
class ProductConfig:
    """Validated, resolved product configuration snapshot.

    `api_key` holds the resolved secret in memory only; it is
    never rendered by `to_redacted_dict`, `summary_lines`, or any
    error message (errors always cite the environment variable
    name, never its value).
    """

    provider: str            # ollama | openai | anthropic
    mode: str                # effective: local | remote
    model: str
    base_url: str
    api_key_env: str         # env var name that holds the key
    api_key: str = ""        # resolved literal (memory only)
    api_key_file: Optional[str] = None  # owner-only local gateway key
    profile: str = "local"
    host: str = "127.0.0.1"
    port: int = 8844
    log_level: str = "INFO"
    source_path: Optional[str] = None
    warnings: list = field(default_factory=list)

    @property
    def style(self) -> str:
        """Runtime provider style; matches LocalLLMProvider styles."""
        return self.provider

    def to_redacted_dict(self) -> dict:
        """Non-secret view, safe for display, logs, and /status."""
        return {
            "provider": self.provider,
            "mode": self.mode,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env or None,
            "api_key": "set" if self.api_key else "not set",
            "api_key_file": self.api_key_file,
            "profile": self.profile,
            "host": self.host,
            "port": self.port,
            "log_level": self.log_level,
            "source_path": self.source_path,
            "warnings": list(self.warnings),
        }

    def summary_lines(self) -> list[str]:
        """Human-readable non-secret summary for `yodaw config show`."""
        return [
            f"provider  = {self.provider}",
            f"mode      = {self.mode}",
            f"model     = {self.model}",
            f"base_url  = {self.base_url}",
            f"api_key   = {'set via ' + self.api_key_env if self.api_key_env else 'set' if self.api_key else 'not set'}"
            + (f" (file: {self.api_key_file})" if self.api_key_file else ""),
            f"profile   = {self.profile}",
            f"host      = {self.host}",
            f"port      = {self.port}",
            f"log_level = {self.log_level}",
            f"source    = {self.source_path or '(environment + defaults)'}",
        ]


# ------------------------------------------------------------
# File discovery
# ------------------------------------------------------------

def config_file_candidates() -> list[str]:
    """Candidate paths in priority order; later files may not exist."""
    explicit = os.environ.get("YODAW_CONFIG")

    if explicit:
        return [explicit]

    home = os.path.expanduser("~")
    xdg = os.environ.get(
        "XDG_CONFIG_HOME", os.path.join(home, ".config")
    )

    return [
        os.path.join(home, "Library", "Application Support", "yodaw", _CONFIG_TEMPLATE_NAME),
        os.path.join(xdg, "yodaw", _CONFIG_TEMPLATE_NAME),
        os.path.join(os.getcwd(), "yodaw.config.toml"),
    ]


def find_config_file() -> Optional[str]:
    candidates = config_file_candidates()

    explicit = os.environ.get("YODAW_CONFIG")

    if explicit:
        # An explicit path is authoritative: a missing file is an
        # error, not a silent fall-through to the defaults.
        if not os.path.isfile(explicit):
            raise ConfigError(
                f"YODAW_CONFIG points at {explicit!r}, but that "
                "file does not exist"
            )
        return explicit

    return next(
        (path for path in candidates if os.path.isfile(path)),
        None,
    )


# ------------------------------------------------------------
# Loading + validation
# ------------------------------------------------------------

def _precedence(env_names, file_value, default):
    """Resolve one field: env vars (in order) > file > default."""
    for name in env_names:
        raw = os.environ.get(name)
        if raw is not None and raw.strip() != "":
            return raw.strip()
    if file_value is not None and str(file_value).strip() != "":
        return str(file_value).strip()
    return default


def _validate_choice(field_name: str, value: str, allowed, where: str) -> None:
    if value not in allowed:
        raise ConfigError(
            f"invalid {field_name} {value!r} in {where}; expected "
            f"one of: {', '.join(allowed)}"
        )


def _validate_model(model: str, where: str) -> None:
    if not model:
        raise ConfigError(
            f"no model configured in {where}; set [llm] model (or "
            "YODAW_LLM_MODEL) to a model identifier or 'auto' for "
            "the provider default"
        )


def _section(data: dict, name: str) -> dict:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"config section [{name}] must be a table")
    return value


def load_product_config(path: Optional[str] = None) -> ProductConfig:
    """
    Resolve file + environment into a validated ProductConfig.

    Environment variables override the file, which overrides sane
    defaults. Raises ConfigError with an actionable message on the
    first invalid value.
    """
    source_path = path or find_config_file()
    data: dict = {}
    where = "the config file"

    if source_path:
        data = _load_toml(source_path)
        where = f"config file {source_path}"

    llm = _section(data, "llm")
    server = _section(data, "server")
    logging_cfg = _section(data, "logging")

    # --- provider / mode resolution -------------------------
    provider = _precedence(
        (LLM_PROVIDER_ENV, LLM_STYLE_ENV),
        llm.get("provider"),
        "auto",
    )
    _validate_choice("provider", provider, PROVIDERS, where)

    mode_raw = _precedence((LLM_MODE_ENV,), llm.get("mode"), "auto")
    _validate_choice("mode", mode_raw, MODES, where)

    mode = "local" if mode_raw == "auto" else mode_raw

    if provider == "auto":
        provider = "ollama" if mode == "local" else "openai"

    if provider == "ollama" and mode != "local":
        raise ConfigError(
            f"provider 'ollama' is local-only; set [llm] mode = "
            f"'local' (or unset mode) in {where}"
        )

    # --- model / base_url / key env name ---------------------
    model = _precedence((LLM_MODEL_ENV,), llm.get("model"), "auto")
    if model == "auto":
        model = MODEL_DEFAULTS[provider]
    _validate_model(model, where)

    base_url = _precedence(
        (LLM_BASE_URL_ENV,),
        llm.get("base_url"),
        BASE_URL_DEFAULTS[provider],
    ).rstrip("/")

    api_key_env = _precedence(
        (LLM_API_KEY_ENV_NAME_ENV,),
        llm.get("api_key_env"),
        API_KEY_ENV_DEFAULTS[provider],
    )

    # --- API key resolution ----------------------------------
    # Precedence: literal env vars (never persisted), then the
    # owner-only local gateway key file written by zero-touch
    # `yodaw setup-9router`.
    api_key_file = _resolve_key_file(
        _precedence(
            (LLM_API_KEY_FILE_ENV,), llm.get("api_key_file"), ""
        ),
        source_path,
    )
    file_key = ""
    key_file_warnings: list[str] = []
    env_key = (
        os.environ.get(LLM_API_KEY_ENV_VAR)
        or os.environ.get(api_key_env)
        or ""
    )
    if api_key_file:
        if not os.path.isfile(api_key_file):
            if env_key:
                # Environment wins; a stale file reference degrades
                # to a warning rather than blocking the env-provided key.
                key_file_warnings.append(
                    f"api_key_file {api_key_file!r} does not exist; "
                    f"falling back to {api_key_env or LLM_API_KEY_ENV_VAR}"
                )
            else:
                raise ConfigError(
                    f"{where} references api_key_file {api_key_file!r}, "
                    "but that file does not exist"
                )
        else:
            warning = _harden_key_file_permissions(api_key_file)
            if warning:
                key_file_warnings.append(warning)
            try:
                file_key = _read_key_file(api_key_file)
            except OSError as exc:
                raise ConfigError(
                    f"cannot read api_key_file {api_key_file!r}: {exc}"
                ) from exc
            if not file_key and not env_key:
                raise ConfigError(
                    f"api_key_file {api_key_file!r} in {where} is empty"
                )

    api_key = env_key or file_key or ""
    needs_key = provider in ("openai", "anthropic", "9router") and not _is_loopback(base_url)
    if needs_key and not api_key:
        raise ConfigError(
            f"{where} selects provider {provider!r}, which requires "
            f"an API key: set the environment variable "
            f"{api_key_env or LLM_API_KEY_ENV_VAR} (the default key "
            f"variable for {provider})"
        )

    # --- server / logging ------------------------------------
    profile = _precedence(("YODAW_PROFILE",), server.get("profile"), "local")
    _validate_choice("profile", profile, PROFILES, where)

    host = _precedence(("YODAW_HOST",), server.get("host"), "127.0.0.1")
    if not host:
        raise ConfigError(f"invalid server host {host!r} in {where}")

    port_raw = _precedence(("YODAW_PORT",), server.get("port"), "8844")
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"invalid server port {port_raw!r} in {where}; expected "
            "an integer (1-65535)"
        )
    if not 1 <= port <= 65535:
        raise ConfigError(
            f"server port {port} in {where} is out of range (1-65535)"
        )

    log_level = _precedence(
        ("YODAW_LOG_LEVEL",), logging_cfg.get("level"), "INFO"
    ).upper()
    _validate_choice("log_level", log_level, LOG_LEVELS, where)

    warnings: list[str] = []
    warnings.extend(key_file_warnings)
    if (
        mode == "local"
        and provider in ("openai", "anthropic", "9router")
        and not _is_loopback(base_url)
    ):
        warnings.append(
            f"mode is 'local' but the endpoint {base_url!r} is a "
            "hosted API; set [llm] mode = 'remote'"
        )
    if _is_loopback(base_url) and not api_key and provider in ("openai", "anthropic"):
        warnings.append(
            f"local endpoint {base_url!r} has no API key; local "
            "servers usually do not require one"
        )
    if provider == "9router" and not api_key:
        warnings.append(
            "9Router requires its local gateway API key even on "
            "localhost; run `yodaw setup-9router` to provision it "
            "automatically, or copy it from "
            f"{base_url.rstrip('/')}/dashboard into the "
            f"{api_key_env or 'NINEROUTER_API_KEY'} environment variable"
        )

    return ProductConfig(
        provider=provider,
        mode=mode,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key=api_key,
        api_key_file=api_key_file,
        profile=profile,
        host=host,
        port=port,
        log_level=log_level,
        source_path=source_path,
        warnings=warnings,
    )


# ------------------------------------------------------------
# Validation (collect-all, for `yodaw config validate`)
# ------------------------------------------------------------

def validate_product_config(path: Optional[str] = None) -> list[str]:
    """Run every validation and return all errors (empty = valid)."""
    errors: list[str] = []
    try:
        load_product_config(path)
    except ConfigError as exc:
        errors.append(str(exc))
    except OSError as exc:
        errors.append(f"cannot read config: {exc}")
    return errors


# ------------------------------------------------------------
# Environment bridge (backwards compatible: env always wins)
# ------------------------------------------------------------

def apply_product_config(path: Optional[str] = None) -> list[str]:
    """
    Materialize file-based config into the environment the runtime
    already reads. Keys already present in the environment are left
    untouched, so explicit environment overrides and legacy setups
    keep working unchanged. No-op when no config file exists.

    Returns the names of the environment variables that were set.
    """
    source = path or find_config_file()
    if not source:
        return []

    cfg = load_product_config(source)
    applied: list[str] = []

    runtime_values = {
        LLM_STYLE_ENV: cfg.style,
        LLM_BASE_URL_ENV: cfg.base_url,
        LLM_MODEL_ENV: cfg.model,
        LLM_API_KEY_ENV_VAR: cfg.api_key,
    }

    for name, value in runtime_values.items():
        if name not in os.environ and value:
            os.environ[name] = value
            applied.append(name)

    # Server / logging keys only when the file declares them.
    data = _load_toml(source)
    declared = {
        "YODAW_PROFILE": _section(data, "server").get("profile"),
        "YODAW_HOST": _section(data, "server").get("host"),
        "YODAW_PORT": _section(data, "server").get("port"),
        "YODAW_LOG_LEVEL": _section(data, "logging").get("level"),
    }
    for name, value in declared.items():
        if value is not None and name not in os.environ:
            os.environ[name] = str(value)
            applied.append(name)

    return applied


# ------------------------------------------------------------
# First-run setup
# ------------------------------------------------------------

_TEMPLATE = """\
# YODAW configuration
# Created by `yodaw config init`. Environment variables always win.
# Secrets are never stored here; `api_key_env` names the environment
# variable that holds the key.

[llm]
# provider: ollama | openai | anthropic | 9router | auto
# 9router = local 9Router daemon (http://127.0.0.1:20128); run
# `yodaw setup-9router` for zero-touch provisioning.
provider = "auto"

# mode: local (machine-local endpoint) | remote (hosted API) | auto
mode = "auto"

# model: model identifier, or "auto" for the provider default
# (for 9router, "auto" detects combos/models from GET /v1/models)
model = "auto"

# base_url: override the provider endpoint (optional)
# base_url = "http://127.0.0.1:11434"

# api_key_env: environment variable holding the key (not the key)
# api_key_env = "OPENAI_API_KEY"
# 9router default: NINEROUTER_API_KEY (copy from the dashboard)

[server]
profile = "local"
host = "127.0.0.1"
port = 8844

[logging]
level = "INFO"

# Env overrides accepted (env beats file):
#   YODAW_LLM_PROVIDER  YODAW_LLM_MODE  YODAW_LLM_MODEL
#   YODAW_LLM_BASE_URL  YODAW_LLM_API_KEY_ENV
#   YODAW_LLM_API_KEY (literal key, legacy)  YODAW_PROFILE
#   YODAW_HOST  YODAW_PORT  YODAW_LOG_LEVEL
"""


def write_default_config(path: Optional[str] = None, force: bool = False) -> str:
    """Write a commented starter config; returns the path written."""
    target = path or config_file_candidates()[0]

    if os.path.exists(target) and not (force or _same_file(target)):
        raise ConfigError(
            f"config file {target} already exists; edit it or use "
            "`yodaw config init --force` to overwrite"
        )

    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)

    with open(target, "w") as handle:
        handle.write(_TEMPLATE)

    return target


def _same_file(target: str) -> bool:
    """True when the existing file was written by this template."""
    try:
        with open(target) as handle:
            return handle.read().startswith("# YODAW configuration")
    except OSError:
        return False


# ------------------------------------------------------------
# Persistent LLM updates (zero-touch provisioning)
# ------------------------------------------------------------

_LLM_KEYS = (
    "provider",
    "mode",
    "model",
    "base_url",
    "api_key_env",
    "api_key_file",
)
_KNOWN_SECTIONS = ("llm", "server", "logging")


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _dump_toml(data: dict) -> str:
    """Serialize our small config schema back to TOML.

    Only string/int/bool scalars in tables are supported, which is
    exactly what this schema uses. Known sections keep a stable
    order; anything else is preserved verbatim after them.
    """
    lines = ["# YODAW configuration", ""]
    seen: set[str] = set()
    for section in (*_KNOWN_SECTIONS, *sorted(data)):
        if section in seen:
            continue
        seen.add(section)
        table = data.get(section)
        if not isinstance(table, dict):
            continue
        if section == "llm":
            keys = [k for k in _LLM_KEYS if k in table]
            keys += sorted(k for k in table if k not in _LLM_KEYS)
        else:
            keys = sorted(table)
        if not keys and section not in _KNOWN_SECTIONS:
            continue
        lines.append(f"[{section}]")
        for key in keys:
            lines.append(f"{key} = {_toml_value(table[key])}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def write_llm_config(
    path: Optional[str] = None,
    *,
    provider: Optional[str] = None,
    mode: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key_env: Optional[str] = None,
    api_key_file: Optional[str] = None,
) -> str:
    """Persist [llm] keys into the config file (create or update).

    Missing files are created from the default template first, so
    provisioning never requires manual file editing or env exports
    for provider/model/endpoint selection. Upstream secrets are
    never written: only the ``api_key_env`` variable *name* is
    stored; ``api_key_file`` points at an owner-only file holding
    the LOCAL 9Router gateway key (see write_api_key_file). Other
    sections are preserved. Returns the path written.
    """
    updates = {
        "provider": provider,
        "mode": mode,
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env,
        "api_key_file": api_key_file,
    }
    updates = {k: v for k, v in updates.items() if v is not None}

    if provider is not None and provider not in PROVIDERS:
        raise ConfigError(
            f"invalid provider {provider!r}; expected one of: "
            f"{', '.join(PROVIDERS)}"
        )
    if mode is not None and mode not in MODES:
        raise ConfigError(
            f"invalid mode {mode!r}; expected one of: {', '.join(MODES)}"
        )

    target = path or config_file_candidates()[0]
    if not os.path.isfile(target):
        write_default_config(target, force=True)

    data = _load_toml(target)
    llm = dict(data.get("llm") or {})
    llm.update(updates)
    data["llm"] = llm

    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    with open(target, "w") as handle:
        handle.write(_dump_toml(data))

    # Fail fast on our own write: the file must load cleanly.
    load_product_config(target)
    return target


# ------------------------------------------------------------
# Redaction: no secrets in logs or error text
# ------------------------------------------------------------

_SECRET_KEY_TOKENS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
)


def redact_text(text: str, secrets: tuple[str, ...]) -> str:
    """Replace every configured secret value inside a string."""
    if not text:
        return text
    redacted = text
    for value in secrets:
        if value:
            redacted = redacted.replace(value, REDACTED)
    return redacted


def redact_config(obj, secrets: Optional[tuple[str, ...]] = None) -> object:
    """Deep-copy a config-shaped object with secret material masked."""
    secrets = secrets or ()
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            lowered = str(key).lower()
            if any(token in lowered for token in _SECRET_KEY_TOKENS):
                out[key] = REDACTED
            else:
                out[key] = redact_config(value, secrets)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_config(item, secrets) for item in obj]
    if isinstance(obj, str):
        return redact_text(obj, secrets)
    return obj