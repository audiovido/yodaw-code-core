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
        data = response.json()
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
