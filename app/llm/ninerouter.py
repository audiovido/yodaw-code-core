"""Native 9Router provider support.

9Router (https://github.com/decolua/9router) is a local AI router that
exposes one OpenAI-compatible endpoint for 40+ backend providers::

    Dashboard: http://127.0.0.1:20128/dashboard
    API:       http://127.0.0.1:20128/v1

YODAW speaks plain OpenAI ``/v1/chat/completions`` with ``stream=false``,
lists models *and* combos via ``GET /v1/models``, and auto-selects a
default model when the configured model is ``"auto"``.

Install::

    npm install -g 9router
    9router            # dashboard opens; copy the API key

Then either zero-touch::

    yodaw setup-9router

or manually::

    yodaw config set --provider 9router
    export NINEROUTER_API_KEY='<key from the dashboard>'

This module never touches the environment: callers pass base_url /
api_key explicitly, so it stays hermetic and trivially testable. All
network access goes through the module-level ``httpx`` attribute so
tests can monkeypatch it with a fake transport.
"""

from __future__ import annotations

import shutil
from typing import Optional

import httpx
from urllib.parse import urlparse

DEFAULT_BASE_URL = "http://127.0.0.1:20128"
DEFAULT_PORT = 20128
DEFAULT_API_KEY_ENV = "NINEROUTER_API_KEY"

MODELS_PATH = "/v1/models"
CHAT_PATH = "/v1/chat/completions"

PROBE_TIMEOUT_SECONDS = 10.0
DETECT_TIMEOUT_SECONDS = 10.0


class NinerouterError(RuntimeError):
    """The 9Router endpoint failed deterministically or is unreachable."""


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes and a trailing /v1 (callers re-append it)."""
    text = (url or "").strip().rstrip("/")
    if text.endswith("/v1"):
        text = text[: -len("/v1")]
    return text.rstrip("/") or DEFAULT_BASE_URL


def _auth_headers(api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if (api_key or "").strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    return headers


def build_chat_request(
    base_url: str,
    model: str,
    system: str,
    user: str,
    api_key: str = "",
    stream: bool = False,
) -> tuple[str, dict, dict]:
    """Build the OpenAI-compatible chat request 9Router expects."""
    url = f"{normalize_base_url(base_url)}{CHAT_PATH}"
    payload = {
        "model": model,
        "stream": stream,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    return url, payload, _auth_headers(api_key)


def lenient_json_loads(text: str) -> dict:
    """Parse a JSON body, tolerating trailing SSE keepalive markers.

    Shipping 9Router versions append a literal ``data: [DONE]``
    trailer to some non-streaming proxied responses, which makes a
    strict ``json.loads`` fail with "Extra data". We extract the
    outermost JSON object instead; anything else still raises.
    """
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise NinerouterError("response body was not a JSON object")
    return parsed


def parse_chat_response(data: object) -> str:
    """Extract the assistant text from an OpenAI-compatible response."""
    try:
        assert isinstance(data, dict)
        choices = data["choices"]
        assert isinstance(choices, list) and choices
        message = choices[0]["message"]
        content = message["content"]
        assert isinstance(content, str)
        return content
    except (KeyError, IndexError, TypeError, AssertionError) as exc:
        raise NinerouterError(
            f"9Router response malformed (expected OpenAI-style "
            f"choices[0].message.content): {exc}"
        ) from exc


def _split_models(ids: list[str]) -> tuple[list[str], list[str]]:
    """Split /v1/models ids into (models, combos).

    9Router returns combos (routing presets such as ``premium-coding``)
    alongside concrete models (``kr/claude-sonnet-4.5``) in one list.
    Concrete models always carry a ``provider/name`` prefix; combos are
    bare names.
    """
    models = sorted({i for i in ids if "/" in i})
    combos = sorted({i for i in ids if "/" not in i})
    return models, combos


def list_models(
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "",
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """List 9Router models and combos.

    Returns ``{"models": [...], "combos": [...], "base_url": ...}``.
    Raises NinerouterError with an actionable message on any failure.
    """
    url = f"{normalize_base_url(base_url)}{MODELS_PATH}"
    try:
        response = httpx.get(
            url, headers=_auth_headers(api_key), timeout=timeout
        )
        response.raise_for_status()
        data = response.json()
    except NinerouterError:
        raise
    except Exception as exc:
        raise NinerouterError(
            f"cannot reach 9Router at {url}: {exc}. Is 9Router running? "
            f"Start it with `9router` (dashboard: "
            f"{normalize_base_url(base_url)}/dashboard)."
        ) from exc

    try:
        assert isinstance(data, dict)
        entries = data["data"]
        assert isinstance(entries, list)
        ids = [entry["id"] for entry in entries]
        assert all(isinstance(i, str) for i in ids)
    except (KeyError, TypeError, AssertionError) as exc:
        raise NinerouterError(
            f"9Router /v1/models response malformed "
            f"(expected OpenAI-style {{data: [{{id}}]}}): {exc}"
        ) from exc

    models, combos = _split_models(ids)
    return {
        "models": models,
        "combos": combos,
        "base_url": normalize_base_url(base_url),
    }


def pick_default_model(models: list[str], combos: list[str]) -> str:
    """Deterministically pick the best default model-or-combo.

    Preference: a coding-flavoured combo first (combos route across
    providers with automatic fallback, which is exactly what YODAW
    wants), then any combo, then a preferred free/code model, then the
    first id alphabetically. Raises NinerouterError when empty.
    """
    combos = sorted(combos or [])
    models = sorted(models or [])

    if combos:
        coding = [c for c in combos if "cod" in c.lower()]
        return sorted(coding)[0] if coding else combos[0]

    if models:
        for prefix in ("kr/claude", "cc/claude", "oc/", "kr/"):
            matches = [m for m in models if m.startswith(prefix)]
            if matches:
                return sorted(matches)[0]
        return models[0]

    raise NinerouterError(
        "9Router reports no models and no combos. Connect a provider "
        "in the dashboard (Providers -> Connect Kiro AI or OpenCode "
        "Free), then retry. If a provider IS connected (e.g. "
        "ollama-local, whose models some 9Router versions omit from "
        "/v1/models), pin the model explicitly instead of auto: "
        "`yodaw setup-9router --model <provider/model>`."
    )


_model_cache: dict[str, str] = {}


def clear_model_cache() -> None:
    """Drop cached auto-detected models (tests + explicit refresh)."""
    _model_cache.clear()


def resolve_model(
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "",
    configured: str = "auto",
    timeout: float = DETECT_TIMEOUT_SECONDS,
) -> str:
    """Resolve the effective model id.

    An explicit model is returned untouched (no network). ``"auto"``
    (or empty) triggers one ``GET /v1/models`` + deterministic pick,
    cached per base_url for the process lifetime.
    """
    wanted = (configured or "").strip()
    if wanted and wanted != "auto":
        return wanted

    key = normalize_base_url(base_url)
    if key not in _model_cache:
        listing = list_models(base_url, api_key, timeout=timeout)
        _model_cache[key] = pick_default_model(
            listing["models"], listing["combos"]
        )
    return _model_cache[key]


def probe(
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "",
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """Reachability + inventory check. Never raises.

    Returns ``{"ok": True, "models": [...], "combos": [...],
    "default_model": ..., "base_url": ...}`` or ``{"ok": False,
    "error": ..., "base_url": ...}``.
    """
    normalized = normalize_base_url(base_url)
    try:
        listing = list_models(normalized, api_key, timeout=timeout)
    except NinerouterError as exc:
        return {"ok": False, "error": str(exc), "base_url": normalized}
    except Exception as exc:  # never let a probe crash callers
        return {
            "ok": False,
            "error": f"9Router probe failed: {exc}",
            "base_url": normalized,
        }

    try:
        default = pick_default_model(listing["models"], listing["combos"])
    except NinerouterError as exc:
        return {
            "ok": True,
            "models": listing["models"],
            "combos": listing["combos"],
            "default_model": None,
            "warning": str(exc),
            "base_url": normalized,
        }

    return {
        "ok": True,
        "models": listing["models"],
        "combos": listing["combos"],
        "default_model": default,
        "base_url": normalized,
    }


def detect_install(
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "",
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> dict:
    """Zero-touch readiness: CLI present? daemon reachable? key set?

    Never raises. Used by ``yodaw setup-9router`` to print the exact
    next step instead of a bare connection error.
    """
    cli = shutil.which("9router")
    status = probe(base_url, api_key, timeout=timeout)
    return {
        "cli": cli,
        "cli_installed": bool(cli),
        "reachable": bool(status.get("ok")),
        "api_key_set": bool((api_key or "").strip()),
        "probe": status,
        "base_url": normalize_base_url(base_url),
    }


def chat(
    system: str,
    user: str,
    model: str = "auto",
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = "",
    timeout: float = 120.0,
) -> str:
    """One-shot 9Router chat (used by the E2E script, not the runtime).

    The runtime path is :class:`app.llm.provider.LocalLLMProvider`
    (bounded retries + attempt log); this helper is a thin direct
    client for scripts and smoke checks.
    """
    effective = resolve_model(base_url, api_key, model)
    url, payload, headers = build_chat_request(
        base_url, effective, system, user, api_key
    )
    try:
        response = httpx.post(
            url, json=payload, headers=headers, timeout=timeout
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = lenient_json_loads(response.text)
    except NinerouterError:
        raise
    except Exception as exc:
        raise NinerouterError(
            f"9Router chat failed at {url} (model {effective!r}): {exc}"
        ) from exc
    try:
        return parse_chat_response(data)
    except NinerouterError as exc:
        raise NinerouterError(
            f"9Router chat failed at {url} (model {effective!r}): {exc}"
        ) from exc


def redacted_headers(api_key: str = "") -> dict:
    """Auth headers with the secret masked (safe for evidence/logs)."""
    headers = _auth_headers(api_key)
    if "Authorization" in headers:
        headers["Authorization"] = "Bearer [REDACTED]"
    return headers


# ============================================================
# Zero-touch local provisioning
#
# This section speaks the *local administration* protocol shipped by
# the official 9Router CLI (see node_modules/9router/src/cli/api):
#
#   * daemon data dir: $DATA_DIR or ~/.9router
#   * machine-id file: <data-dir>/machine-id (written by the daemon)
#   * cli secret file:  <data-dir>/auth/cli-secret (mode 0600; the
#     official CLI creates it with random bytes if it is missing, and
#     the daemon accepts that file - both sides read the same path)
#   * admin header:     x-9r-cli-token = sha256(machineId +
#                       "9r-cli-auth" + secret)[:16]
#
# The gateway API *key* (sk-..., used as a Bearer token on /v1) is a
# LOCAL gateway credential only. It is provisioned or rotated ONLY
# when it is missing / rejected / revoked. It must never be rotated
# to work around upstream provider quotas, rate limits, or account
# restrictions - those are handled by legitimately configured
# fallback routes, never by minting fresh local keys.
# ============================================================

import hashlib
import json
import os
import secrets as _secrets
import socket
import subprocess
import time
from pathlib import Path

CLI_TOKEN_HEADER = "x-9r-cli-token"
CLI_TOKEN_SALT = "9r-cli-auth"
DEFAULT_KEY_NAME = "yodaw-zero-touch"
# Local llama.cpp / vLLM style nodes usually need no credential;
# 9Router still requires a non-empty apiKey on connection creation,
# so this clearly-labelled placeholder is sent instead of a secret.
LOCAL_PLACEHOLDER_CREDENTIAL = "local-no-auth-required"

ADMIN_TIMEOUT_SECONDS = 10.0
HEALTH_PATH = "/api/health"
KEYS_PATH = "/api/keys"
NODES_PATH = "/api/provider-nodes"
CONNECTIONS_PATH = "/api/providers"

DAEMON_READY_TIMEOUT_SECONDS = 90.0
DAEMON_POLL_INTERVAL_SECONDS = 0.5


class NinerouterAdminError(NinerouterError):
    """A local 9Router administration API call failed."""


def default_data_dir() -> Path:
    """Resolve the 9Router data directory (matches the official CLI)."""
    env = (os.environ.get("DATA_DIR") or "").strip()
    if env:
        return Path(env)
    return Path.home() / ".9router"


def _read_trimmed(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_cli_secret(data_dir: Optional[Path] = None) -> str:
    """Return the shared CLI secret, creating it (mode 0600) if absent.

    This mirrors the official 9Router CLI exactly: 32 random bytes,
    hex encoded, stored under ``<data-dir>/auth/cli-secret`` with
    owner-only permissions. The daemon reads the same file, so a
    secret created before first contact (or before daemon start) is
    accepted without any manual dashboard step.
    """
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    secret_path = data_dir / "auth" / "cli-secret"
    existing = _read_trimmed(secret_path)
    if existing:
        return existing

    value = _secrets.token_hex(32)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(value, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    try:
        os.chmod(secret_path.parent, 0o700)
    except OSError:
        pass
    return value


def read_machine_id(data_dir: Optional[Path] = None) -> str:
    """Read the daemon-written machine id ('' until a daemon ran)."""
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    return _read_trimmed(data_dir / "machine-id")


def system_machine_id() -> str:
    """Compute the OS machine id the way node-machine-id does.

    The official 9Router CLI derives its admin token from the FIRST
    of ``/var/lib/dbus/machine-id`` / ``/etc/machine-id`` (falling
    back to the host name), sha256-hashed, trimmed and lowercased.
    A freshly started daemon has not yet persisted its
    ``machine-id`` file (it writes one lazily on the first admin
    call), so we must be able to compute the same id ourselves.
    """
    raw = ""
    for candidate in ("/var/lib/dbus/machine-id", "/etc/machine-id"):
        raw = _read_trimmed(Path(candidate))
        if raw:
            break
    if not raw:
        try:
            raw = socket.gethostname()
        except OSError:
            return ""
    normalized = "".join(raw.split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def effective_machine_id(data_dir: Optional[Path] = None) -> str:
    """Daemon-persisted id when present, else the computed OS id."""
    return read_machine_id(data_dir) or system_machine_id()


def derive_cli_token(
    data_dir: Optional[Path] = None, *, create_secret: bool = True
) -> str:
    """Derive the local admin token from machine-id + cli-secret.

    Uses the daemon-persisted machine id when available, otherwise
    the computed OS machine id (the daemon writes its file lazily,
    but derives the same value). Returns '' only when no machine id
    can be determined at all.
    """
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    machine_id = effective_machine_id(data_dir)
    if not machine_id:
        return ""
    if create_secret:
        secret = ensure_cli_secret(data_dir)
    else:
        secret = _read_trimmed(data_dir / "auth" / "cli-secret")
    if not secret:
        return ""
    return hashlib.sha256(
        (machine_id + CLI_TOKEN_SALT + secret).encode("utf-8")
    ).hexdigest()[:16]


def _admin_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        CLI_TOKEN_HEADER: token,
    }


def _admin_request(
    method: str,
    path: str,
    base_url: str,
    token: str,
    body: Optional[dict] = None,
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> dict:
    """One authenticated local-admin request.

    Uses the module-level ``httpx`` seam so tests stay hermetic.
    Raises NinerouterAdminError on transport failure, non-2xx, or an
    ``{"error": ...}`` envelope (the official client treats those as
    failures too).
    """
    url = f"{normalize_base_url(base_url)}{path}"
    headers = _admin_headers(token)
    try:
        if method == "GET":
            response = httpx.get(url, headers=headers, timeout=timeout)
        elif method == "POST":
            response = httpx.post(
                url, headers=headers, json=body or {}, timeout=timeout
            )
        elif method == "PUT":
            response = httpx.put(
                url, headers=headers, json=body or {}, timeout=timeout
            )
        elif method == "DELETE":
            response = httpx.delete(url, headers=headers, timeout=timeout)
        else:
            raise NinerouterAdminError(f"unsupported admin method {method!r}")
    except NinerouterError:
        raise
    except Exception as exc:
        raise NinerouterAdminError(
            f"9Router admin call {method} {path} failed: {exc}"
        ) from exc

    status = getattr(response, "status_code", 0)
    try:
        data = response.json()
    except Exception:
        data = {}
    if status >= 400:
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or "")
        raise NinerouterAdminError(
            f"9Router admin call {method} {path} returned HTTP "
            f"{status}{(' - ' + detail) if detail else ''}"
        )
    if isinstance(data, dict) and data.get("error"):
        raise NinerouterAdminError(
            f"9Router admin call {method} {path} failed: {data['error']}"
        )
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------
# Daemon lifecycle
# ------------------------------------------------------------

def daemon_health(
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> bool:
    """True when the local 9Router daemon answers /api/health."""
    url = f"{normalize_base_url(base_url)}{HEALTH_PATH}"
    try:
        response = httpx.get(url, timeout=timeout)
        if getattr(response, "status_code", 0) != 200:
            return False
        data = response.json()
        return bool(isinstance(data, dict) and data.get("ok", True))
    except Exception:
        return False


def find_cli() -> Optional[str]:
    """Locate the 9router CLI on PATH or in common npm global bins."""
    found = shutil.which("9router")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".npm-global" / "bin" / "9router",
        home / ".local" / "share" / "npm-global" / "bin" / "9router",
        Path("/usr/local/bin/9router"),
        Path("/usr/bin/9router"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def find_npm() -> Optional[str]:
    return shutil.which("npm")


def install_cli(timeout: float = 600.0) -> str:
    """Install 9Router globally via the official npm package.

    Zero-touch install path. Requires npm; raises NinerouterAdminError
    with an actionable message otherwise. Never swallows a failed
    install into a fake success.
    """
    existing = find_cli()
    if existing:
        return existing
    npm = find_npm()
    if not npm:
        raise NinerouterAdminError(
            "9Router is not installed and npm was not found on PATH; "
            "install Node.js/npm then rerun, or install manually: "
            "npm install -g 9router"
        )
    try:
        subprocess.run(
            [npm, "install", "-g", "9router"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise NinerouterAdminError(
            f"npm install -g 9router failed: "
            f"{(exc.stderr or exc.stdout or '').strip()[-400:]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise NinerouterAdminError(
            f"npm install -g 9router timed out after {timeout:.0f}s"
        ) from exc
    installed = find_cli()
    if not installed:
        raise NinerouterAdminError(
            "npm install -g 9router reported success but the "
            "9router binary is still not on PATH"
        )
    return installed


def start_daemon(
    data_dir: Optional[Path] = None,
    port: int = DEFAULT_PORT,
    log_file: Optional[Path] = None,
) -> int:
    """Start the 9Router daemon detached; return the launcher PID.

    The daemon binds localhost, skips auto-update/browser, and keeps
    running independently of this process (new session + detached
    fds). Callers then poll :func:`daemon_health`.
    """
    cli = find_cli()
    if not cli:
        raise NinerouterAdminError(
            "cannot start 9Router: CLI not found (run install_cli())"
        )
    data_dir = Path(data_dir) if data_dir else default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    # Pre-create the shared admin secret so the daemon we are about
    # to launch derives the same admin token (the official CLI does
    # this too); 0600 owner-only.
    ensure_cli_secret(data_dir)

    env = os.environ.copy()
    env["DATA_DIR"] = str(data_dir)
    env["PORT"] = str(port)
    env["TRAY_MODE"] = "1"

    log_handle = None
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_file, "ab")
    try:
        process = subprocess.Popen(
            [
                cli,
                "--no-browser",
                "--skip-update",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(data_dir),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle if log_handle else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        if log_handle is not None:
            log_handle.close()
    return process.pid


def ensure_daemon(
    base_url: str = DEFAULT_BASE_URL,
    data_dir: Optional[Path] = None,
    port: int = DEFAULT_PORT,
    timeout: float = DAEMON_READY_TIMEOUT_SECONDS,
    install: bool = False,
    log_file: Optional[Path] = None,
) -> dict:
    """Ensure a reachable local daemon, starting/installing if allowed.

    Never raises: returns ``{"running", "started", "pid", "error"}``.
    """
    if daemon_health(base_url):
        return {
            "running": True,
            "started": False,
            "pid": None,
            "base_url": normalize_base_url(base_url),
        }

    cli = find_cli()
    if not cli and install:
        try:
            install_cli()
            cli = find_cli()
        except NinerouterAdminError as exc:
            return {
                "running": False,
                "started": False,
                "pid": None,
                "error": str(exc),
                "base_url": normalize_base_url(base_url),
            }
    if not cli:
        return {
            "running": False,
            "started": False,
            "pid": None,
            "error": (
                "9Router CLI not found; install with "
                "`npm install -g 9router`"
            ),
            "base_url": normalize_base_url(base_url),
        }

    try:
        pid = start_daemon(data_dir=data_dir, port=port, log_file=log_file)
    except NinerouterAdminError as exc:
        return {
            "running": False,
            "started": False,
            "pid": None,
            "error": str(exc),
            "base_url": normalize_base_url(base_url),
        }

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if daemon_health(base_url):
            return {
                "running": True,
                "started": True,
                "pid": pid,
                "base_url": normalize_base_url(base_url),
            }
        time.sleep(DAEMON_POLL_INTERVAL_SECONDS)

    return {
        "running": False,
        "started": True,
        "pid": pid,
        "error": (
            f"9Router daemon did not become healthy within {timeout:.0f}s"
        ),
        "base_url": normalize_base_url(base_url),
    }


# ------------------------------------------------------------
# Gateway API keys (local Bearer credentials for /v1)
# ------------------------------------------------------------

def list_gateway_keys(
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> list[dict]:
    data = _admin_request("GET", KEYS_PATH, base_url, token, timeout=timeout)
    keys = data.get("keys", [])
    return keys if isinstance(keys, list) else []


def create_gateway_key(
    name: str = DEFAULT_KEY_NAME,
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> dict:
    data = _admin_request(
        "POST", KEYS_PATH, base_url, token, body={"name": name},
        timeout=timeout,
    )
    record = data.get("key")
    if isinstance(record, dict) and record.get("key"):
        return record
    if isinstance(data, dict) and data.get("key") and isinstance(
        data.get("key"), str
    ):
        return {
            "id": data.get("id"),
            "key": data["key"],
            "name": data.get("name", name),
        }
    raise NinerouterAdminError(
        f"9Router created no usable gateway key for {name!r}: {data!r}"
    )


def delete_gateway_key(
    key_id: str,
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> None:
    _admin_request(
        "DELETE", f"{KEYS_PATH}/{key_id}", base_url, token, timeout=timeout
    )


CHAT_VALIDATION_MAX_TOKENS = 1


def validate_gateway_key(
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = ADMIN_TIMEOUT_SECONDS,
    model: Optional[str] = None,
) -> bool:
    """True when the Bearer key authenticates against the gateway.

    Shipping 9Router enforces ``requireApiKey`` on chat completions
    (401 for missing/unknown/revoked keys) while leaving the model
    inventory open, so the cheapest trustworthy proof is a
    max_tokens=1 chat. When no model is wired yet the proof is
    inconclusive and the key is treated as valid - a missing route
    must never trigger credential rotation.

    Only 401/403 means invalid; every other outcome (5xx, timeout,
    empty inventory) returns True so transient faults never mint
    replacement keys.
    """
    key = (api_key or "").strip()
    if not key:
        return False
    root = normalize_base_url(base_url)

    chosen = (model or "").strip()
    if not chosen or chosen == "auto":
        try:
            listing = list_models(root, key, timeout=timeout)
            inventory = listing["combos"] + listing["models"]
            chosen = inventory[0] if inventory else ""
        except NinerouterError:
            return True  # inconclusive: do not rotate
    if not chosen:
        return True  # no route to prove against: do not rotate

    url = f"{root}{CHAT_PATH}"
    payload = {
        "model": chosen,
        "max_tokens": CHAT_VALIDATION_MAX_TOKENS,
        "messages": [{"role": "user", "content": "ping"}],
    }
    try:
        response = httpx.post(
            url,
            json=payload,
            headers=_auth_headers(key),
            timeout=timeout,
        )
    except Exception:
        return True
    return getattr(response, "status_code", 200) not in (401, 403)


def provision_gateway_key(
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    name: str = DEFAULT_KEY_NAME,
    existing_key: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> dict:
    """Idempotently provision a usable LOCAL gateway key.

    Policy:

    * ``existing_key`` validates against /v1 -> reused untouched (no
      churn, no rotation, ever).
    * otherwise an active key with this name is looked up: if it
      validates, its plaintext (9Router returns it) is reused; if it
      is rejected/revoked it is deleted and replaced - rotation
      happens ONLY for invalid local credentials, never to dodge
      upstream quotas/rate limits.
    * otherwise a fresh key with this name is minted.

    Returns ``{"key", "id", "name", "reused"|"created"|"rotated"}``.
    """
    existing_key = (existing_key or "").strip()
    if existing_key and validate_gateway_key(
        existing_key, base_url, timeout=timeout
    ):
        return {
            "key": existing_key,
            "id": None,
            "name": name,
            "status": "reused",
        }

    match = None
    for record in list_gateway_keys(base_url, token, timeout=timeout):
        if (
            record.get("name") == name
            and record.get("isActive", True)
            and record.get("key")
        ):
            match = record
            break

    if match:
        if validate_gateway_key(
            match["key"], base_url, timeout=timeout
        ):
            return {
                "key": match["key"],
                "id": match.get("id"),
                "name": name,
                "status": "reused",
            }
        # Named key exists but is rejected/revoked: local credential
        # repair only. Delete the dead record, then mint a replacement.
        if match.get("id"):
            try:
                delete_gateway_key(
                    match["id"], base_url, token, timeout=timeout
                )
            except NinerouterAdminError:
                pass
        record = create_gateway_key(
            name, base_url, token, timeout=timeout
        )
        return {
            "key": record["key"],
            "id": record.get("id"),
            "name": name,
            "status": "rotated",
        }

    record = create_gateway_key(name, base_url, token, timeout=timeout)
    return {
        "key": record["key"],
        "id": record.get("id"),
        "name": name,
        "status": "created",
    }


# ------------------------------------------------------------
# Custom provider nodes + connections (local model servers)
# ------------------------------------------------------------

def list_provider_nodes(
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> list[dict]:
    data = _admin_request("GET", NODES_PATH, base_url, token, timeout=timeout)
    nodes = data.get("nodes", [])
    return nodes if isinstance(nodes, list) else []


def create_provider_node(
    *,
    name: str,
    prefix: str,
    node_base_url: str,
    node_type: str = "openai-compatible",
    api_type: str = "chat",
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> dict:
    body = {
        "name": name,
        "prefix": prefix,
        "baseUrl": node_base_url.rstrip("/"),
        "type": node_type,
        "apiType": api_type,
    }
    data = _admin_request(
        "POST", NODES_PATH, base_url, token, body=body, timeout=timeout
    )
    node = data.get("node")
    if not isinstance(node, dict) or not node.get("id"):
        raise NinerouterAdminError(
            f"9Router created no usable provider node: {data!r}"
        )
    return node


def ensure_provider_node(
    *,
    name: str,
    prefix: str,
    node_base_url: str,
    node_type: str = "openai-compatible",
    api_type: str = "chat",
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> dict:
    """Idempotent node registration (9Router allows duplicate prefixes,
    so we dedupe on prefix ourselves). Returns ``{"node", "created"}``.
    """
    wanted_url = node_base_url.rstrip("/")
    for node in list_provider_nodes(base_url, token, timeout=timeout):
        if node.get("prefix") == prefix:
            if node.get("baseUrl", "").rstrip("/") != wanted_url:
                raise NinerouterAdminError(
                    f"provider prefix {prefix!r} already exists with "
                    f"baseUrl {node.get('baseUrl')!r}; refusing to "
                    f"register a conflicting node {wanted_url!r}"
                )
            return {"node": node, "created": False}
    node = create_provider_node(
        name=name,
        prefix=prefix,
        node_base_url=node_base_url,
        node_type=node_type,
        api_type=api_type,
        base_url=base_url,
        token=token,
        timeout=timeout,
    )
    return {"node": node, "created": True}


def list_provider_connections(
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> list[dict]:
    data = _admin_request(
        "GET", CONNECTIONS_PATH, base_url, token, timeout=timeout
    )
    connections = data.get("connections", [])
    return connections if isinstance(connections, list) else []


def create_provider_connection(
    *,
    node_id: str,
    connection_name: str,
    api_key: str = LOCAL_PLACEHOLDER_CREDENTIAL,
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> dict:
    body = {"provider": node_id, "name": connection_name, "apiKey": api_key}
    data = _admin_request(
        "POST", CONNECTIONS_PATH, base_url, token, body=body, timeout=timeout
    )
    connection = data.get("connection")
    if not isinstance(connection, dict) or not connection.get("id"):
        raise NinerouterAdminError(
            f"9Router created no usable provider connection: {data!r}"
        )
    return connection


def ensure_provider_connection(
    *,
    node_id: str,
    connection_name: str,
    api_key: str = LOCAL_PLACEHOLDER_CREDENTIAL,
    base_url: str = DEFAULT_BASE_URL,
    token: str = "",
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> dict:
    """Idempotent connection registration.

    Deduping depends on the credential kind:

    * placeholder (local, no-auth) connections are matched by node
      alone - one local connection per node is sufficient, so
      re-running provisioning never piles up duplicates;
    * named credentials are matched by (node, name) so multiple
      distinct keys for one provider can coexist.

    Returns ``{"connection", "created"}``.
    """
    connections = list_provider_connections(
        base_url, token, timeout=timeout
    )
    local_no_auth = api_key == LOCAL_PLACEHOLDER_CREDENTIAL
    for connection in connections:
        if connection.get("provider") != node_id:
            continue
        if connection.get("isActive") is False:
            continue
        if local_no_auth:
            return {"connection": connection, "created": False}
        if connection.get("name") == connection_name:
            return {"connection": connection, "created": False}
    connection = create_provider_connection(
        node_id=node_id,
        connection_name=connection_name,
        api_key=api_key,
        base_url=base_url,
        token=token,
        timeout=timeout,
    )
    return {"connection": connection, "created": True}


# ------------------------------------------------------------
# End-to-end zero-touch orchestration
# ------------------------------------------------------------

def _try_admin_token(
    base_url: str,
    data_dir: Path,
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> Optional[str]:
    """Derive the token for a data dir; return it only if accepted."""
    # Read-only: never create secret files while probing candidate
    # data directories (creation happens in start_daemon).
    token = derive_cli_token(data_dir, create_secret=False)
    if not token:
        return None
    try:
        list_gateway_keys(base_url, token, timeout=timeout)
    except NinerouterAdminError:
        return None
    return token


def discover_running_data_dir(port: int = DEFAULT_PORT) -> Optional[Path]:
    """Find the DATA_DIR of a running 9Router via /proc (Linux).

    Same-user processes expose command line and environment in
    ``/proc``; the 9Router launcher (and its next-server child) carry
    ``DATA_DIR``. Returns None on non-Linux, when nothing matches, or
    when the process belongs to another user - we must never administer
    another user's daemon.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        if "9router" not in cmdline:
            continue
        try:
            environ = (entry / "environ").read_bytes().decode(
                "utf-8", "replace"
            )
        except OSError:
            continue
        for item in environ.split("\x00"):
            if item.startswith("DATA_DIR="):
                value = item[len("DATA_DIR="):].strip()
                if value and Path(value).is_dir():
                    return Path(value)
    return None


def _candidate_data_dirs(explicit: Path, port: int) -> list[Path]:
    candidates: list[Path] = []
    seen: set[str] = set()

    def add(path: Optional[Path]) -> None:
        if path is None:
            return
        path = Path(path)
        if str(path) not in seen:
            seen.add(str(path))
            candidates.append(path)

    add(explicit)
    env_dir = (os.environ.get("DATA_DIR") or "").strip()
    add(Path(env_dir) if env_dir else None)
    add(discover_running_data_dir(port))
    add(default_data_dir())
    return candidates


def resolve_admin(
    base_url: str = DEFAULT_BASE_URL,
    data_dir: Optional[Path] = None,
    *,
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> tuple[Path, str]:
    """Resolve the data dir the live daemon actually uses + valid token.

    Tries the requested dir first, then DATA_DIR, then a same-user
    /proc discovery, then the default location - so zero-touch
    installs attach to an already-running daemon instead of minting
    secrets that daemon does not know.
    """
    normalized = normalize_base_url(base_url)
    try:
        port = int(urlparse(normalized).port or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    explicit = Path(data_dir) if data_dir else default_data_dir()

    tried: list[str] = []
    for candidate in _candidate_data_dirs(explicit, port):
        tried.append(str(candidate))
        token = _try_admin_token(normalized, candidate, timeout=timeout)
        if token:
            return candidate, token

    raise NinerouterAdminError(
        "no local 9Router data directory authenticated against "
        f"{normalized}; tried: {', '.join(tried)}. If another 9Router "
        "install owns that daemon, point YODAW at its data directory "
        "via --data-dir or the DATA_DIR environment variable."
    )


def admin_token(
    base_url: str = DEFAULT_BASE_URL,
    data_dir: Optional[Path] = None,
    *,
    timeout: float = ADMIN_TIMEOUT_SECONDS,
) -> str:
    """Verify the local admin token, auto-attaching to a running
    daemon's data dir when the requested one does not authenticate."""
    _, token = resolve_admin(base_url, data_dir, timeout=timeout)
    return token


def auto_provision(
    base_url: str = DEFAULT_BASE_URL,
    *,
    data_dir: Optional[Path] = None,
    port: int = DEFAULT_PORT,
    key_name: str = DEFAULT_KEY_NAME,
    existing_key: str = "",
    local_servers: Optional[list[dict]] = None,
    install: bool = True,
    start: bool = True,
    log_file: Optional[Path] = None,
) -> dict:
    """Run the whole local zero-touch provisioning sequence.

    1. ensure the daemon is installed/running (optionally installing
       and starting it), sharing one data directory;
    2. derive + verify the local admin token;
    3. provision (never needlessly rotate) a usable LOCAL gateway key;
    4. idempotently register any local OpenAI-compatible servers.

    Returns a JSON-safe summary. Raises NinerouterAdminError on a
    deterministic failure with an actionable message. This function
    never touches upstream provider credentials and never rotates
    keys to bypass upstream quotas or rate limits.
    """
    data_dir = Path(data_dir) if data_dir else default_data_dir()

    daemon = {"running": False, "started": False}
    if start:
        daemon = ensure_daemon(
            base_url=base_url,
            data_dir=data_dir,
            port=port,
            install=install,
            log_file=log_file,
        )
        if not daemon.get("running"):
            raise NinerouterAdminError(
                "9Router daemon is not reachable and could not be "
                f"started: {daemon.get('error', 'unknown error')}"
            )
    elif not daemon_health(base_url):
        raise NinerouterAdminError(
            f"9Router daemon at {normalize_base_url(base_url)} is not "
            "reachable and start was disabled"
        )

    # The live daemon may already run under a different data dir
    # (e.g. a prior manual install); resolve_admin attaches to it.
    data_dir, token = resolve_admin(base_url, data_dir)

    key_result = provision_gateway_key(
        base_url=base_url,
        token=token,
        name=key_name,
        existing_key=existing_key,
    )

    registered: list[dict] = []
    for server in local_servers or []:
        try:
            name = server["name"]
            prefix = server["prefix"]
            node_url = server["base_url"]
        except (KeyError, TypeError) as exc:
            raise NinerouterAdminError(
                f"local server entry {server!r} must define "
                "name, prefix and base_url"
            ) from exc
        node_outcome = ensure_provider_node(
            name=name,
            prefix=prefix,
            node_base_url=node_url,
            node_type=server.get("type", "openai-compatible"),
            api_type=server.get("api_type", "chat"),
            base_url=base_url,
            token=token,
        )
        node = node_outcome["node"]
        connection_name = server.get(
            "connection_name", f"{prefix}-local"
        )
        connection_outcome = ensure_provider_connection(
            node_id=node["id"],
            connection_name=connection_name,
            api_key=server.get(
                "api_key", LOCAL_PLACEHOLDER_CREDENTIAL
            ),
            base_url=base_url,
            token=token,
        )
        registered.append(
            {
                "name": name,
                "prefix": prefix,
                "base_url": node_url.rstrip("/"),
                "node_id": node["id"],
                "node_created": node_outcome["created"],
                "connection_id": connection_outcome["connection"]["id"],
                "connection_created": connection_outcome["created"],
            }
        )

    return {
        "base_url": normalize_base_url(base_url),
        "data_dir": str(data_dir),
        "daemon": {
            "running": daemon.get("running", daemon_health(base_url)),
            "started": daemon.get("started", False),
            "pid": daemon.get("pid"),
        },
        # The raw key rides along in-process so the caller can
        # persist it to an owner-only file. CLI/JSON renderers must
        # redact it (see _provisioning_summary in app.cli.main).
        "gateway_key": {
            "id": key_result.get("id"),
            "name": key_result["name"],
            "status": key_result["status"],
            "key": key_result["key"],
        },
        "local_servers": registered,
    }


def parse_local_server_spec(spec: str) -> dict:
    """Parse a ``Name:PREFIX:http://host/v1`` CLI/env spec."""
    # The URL itself contains colons (http://host:port), so peel off
    # the name and prefix from the left and treat the remainder as URL.
    try:
        name, rest = spec.split(":", 1)
        prefix, node_url = rest.split(":", 1)
    except ValueError as exc:
        raise NinerouterAdminError(
            f"invalid local server spec {spec!r}; expected "
            "'Name:prefix:http://host:port/v1'"
        ) from exc
    name, prefix, node_url = name.strip(), prefix.strip(), node_url.strip()
    if not name or not prefix or not node_url.startswith(("http://", "https://")):
        raise NinerouterAdminError(
            f"invalid local server spec {spec!r}; expected "
            "'Name:prefix:http://host:port/v1'"
        )
    return {"name": name, "prefix": prefix, "base_url": node_url}
