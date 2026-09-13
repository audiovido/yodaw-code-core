from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import httpx


def llm_timeout_seconds() -> int:
    """Hard cap for one LLM request, configurable for slow hosts."""
    try:
        return int(
            os.environ.get(
                "YODAW_LLM_TIMEOUT_SECONDS",
                "1200",
            )
        )
    except ValueError:
        return 1200


def llm_keep_alive() -> str:
    """Ollama model residency window between calls."""
    return os.environ.get(
        "YODAW_LLM_KEEP_ALIVE",
        "5m",
    )


def provider_retry_config() -> tuple[int, float]:
    """
    Stage 8.6 provider resilience configuration.

    Returns (max_retries, base_backoff_seconds). Retries are
    bounded and use exponential backoff with jitter.
    """
    try:
        max_retries = int(
            os.environ.get(
                "YODAW_PROVIDER_MAX_RETRIES",
                "3",
            )
        )
    except ValueError:
        max_retries = 3

    try:
        backoff = float(
            os.environ.get(
                "YODAW_PROVIDER_BACKOFF_SECONDS",
                "1.0",
            )
        )
    except ValueError:
        backoff = 1.0

    max_retries = max(0, max_retries)
    backoff = max(0.0, backoff)

    return max_retries, backoff


_attempt_log_local = threading.local()


def _attempt_log() -> list:
    log = getattr(_attempt_log_local, "log", None)

    if log is None:
        log = []
        _attempt_log_local.log = log

    return log


def pop_attempt_log() -> list[dict]:
    """
    Drain the per-thread provider attempt log.

    Workers call this after an LLM interaction so every provider
    attempt becomes evidence. Thread-local because each mission
    executes on one worker thread.
    """
    log = _attempt_log()
    _attempt_log_local.log = []

    return log


def _log_attempt(record: dict) -> None:
    record.setdefault(
        "at", datetime.now(timezone.utc).isoformat()
    )
    _attempt_log().append(record)


def _is_retryable(exc: Exception) -> bool:
    """
    Stage 8.6 retry classification.

    Retry transient provider faults: timeouts, connection
    failures, temporary HTTP 5xx, 429, and 408 (a request timeout
    means the call never ran to completion, so retrying is safe).
    Never retry deterministic errors: authentication, missing/
    unsupported model, bad request, malformed responses.
    """
    if isinstance(exc, httpx.TimeoutException):
        return True

    if isinstance(exc, httpx.ConnectError):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status in (429, 408)

    # Malformed payloads and everything else: not retryable.
    return False


def _backoff_delay(base: float, attempt: int) -> float:
    """Exponential backoff with full jitter."""
    if base <= 0:
        return 0.0

    exponential = base * (2 ** (attempt - 1))
    return random.uniform(0, exponential)


class LLMError(RuntimeError):
    pass


ANTHROPIC_API_VERSION = "2023-06-01"
SUPPORTED_STYLES = ("ollama", "openai", "anthropic", "9router")

# Aliases accepted for YODAW_LLM_STYLE / YODAW_LLM_PROVIDER.
STYLE_ALIASES = {
    "ninerouter": "9router",
    "nine_router": "9router",
    "9_router": "9router",
}


def normalize_style(style: str) -> str:
    """Lowercase + alias-resolve a provider style label."""
    text = (style or "").strip().lower()
    return STYLE_ALIASES.get(text, text)


def _normalize_base_url(url: str) -> str:
    """Strip a trailing /v1 so providers re-append it without doubling."""
    return re.sub(r"/v1$", "", url.rstrip("/"))


MODEL_DEFAULTS = {
    "ollama": "qwen2.5-coder:7b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
    # "auto" for 9Router means: detect at chat time via GET
    # /v1/models (combos preferred), cached per base_url.
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


def _sibling(style: str) -> "LocalLLMProvider":
    """Build a fallback provider with that style's own defaults.

    The fallback style inherits no base_url/model/api_key from the
    primary: each backend resolves its own defaults (explicit
    YODAW_LLM_BASE_URL/YODAW_LLM_MODEL still apply when they were
    set for that backend, matching single-provider behavior).
    """
    return LocalLLMProvider(style=style)


def fallback_styles() -> list[str]:
    """Ordered fallback styles from YODAW_LLM_FALLBACKS (may be empty).

    Comma-separated style labels, e.g. ``ollama,openai``. Unknown
    labels are ignored so a typo degrades to fewer fallbacks rather
    than a crash; the primary style itself is always skipped.
    """
    raw = os.environ.get("YODAW_LLM_FALLBACKS", "")
    seen: set[str] = set()
    ordered: list[str] = []
    for part in raw.split(","):
        style = normalize_style(part)
        if not style or style not in SUPPORTED_STYLES:
            continue
        if style in seen:
            continue
        seen.add(style)
        ordered.append(style)
    return ordered


def fallback_models() -> list[str]:
    """Ordered fallback models from YODAW_LLM_FALLBACK_MODELS.

    Comma-separated model/route ids (for the 9router style these
    are ``provider/model`` routes, e.g.
    ``ollama-local/qwen3:0.6b``). Empty entries are dropped and
    duplicates collapse to first occurrence; the primary model
    itself is skipped when the chain is built so listing it here
    is harmless.
    """
    raw = os.environ.get("YODAW_LLM_FALLBACK_MODELS", "")
    seen: set[str] = set()
    ordered: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


class LocalLLMProvider:
    """
    Generic local LLM adapter.

    Supports:
      - Ollama native /api/chat
      - OpenAI-compatible /v1/chat/completions
      - Anthropic /v1/messages
      - 9Router OpenAI-compatible /v1/chat/completions (stream=false)

    Configuration:
      YODAW_LLM_STYLE=ollama|openai|anthropic|9router
      YODAW_LLM_BASE_URL=<base-url>
      YODAW_LLM_MODEL=<model-name>
      YODAW_LLM_API_KEY=<optional>
      YODAW_LLM_FALLBACKS=<comma-separated styles, optional>
      YODAW_LLM_FALLBACK_MODELS=<comma-separated 9router
        routes, optional; each retryable failure fails over to
        the next route instead of hammering one dead route>

    Explicit constructor arguments override the environment (used by
    the fallback chain to build sibling providers with per-style
    defaults); ``LocalLLMProvider()`` keeps the legacy behavior of
    reading everything from the environment.
    """

    def __init__(
        self,
        style: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        try:
            from app.product_config import apply_product_config

            apply_product_config()
        except Exception:
            pass

        env_style = normalize_style(
            os.environ.get("YODAW_LLM_STYLE")
            or os.environ.get("YODAW_LLM_PROVIDER")
            or "ollama"
        )
        self.style = normalize_style(style) if style else env_style

        # A fallback sibling (explicit style differing from the
        # environment's) resolves its OWN defaults: the primary's
        # base_url/model must never leak into another backend.
        foreign = bool(style) and self.style != env_style

        default_base = BASE_URL_DEFAULTS.get(
            self.style, "http://127.0.0.1:11434"
        )
        if base_url:
            raw_base = base_url
        elif foreign:
            raw_base = default_base
        else:
            raw_base = os.environ.get("YODAW_LLM_BASE_URL", default_base)
        self.base_url = _normalize_base_url(raw_base)

        if model is not None:
            wanted = model
        elif foreign:
            wanted = ""
        else:
            wanted = os.environ.get("YODAW_LLM_MODEL", "").strip()
        wanted = (wanted or "").strip()
        if not wanted or wanted == "auto":
            wanted = MODEL_DEFAULTS.get(self.style, "qwen2.5-coder:7b")
        self.model = wanted

        api_key_env = (
            os.environ.get("YODAW_LLM_API_KEY_ENV")
            or API_KEY_ENV_DEFAULTS.get(self.style, "")
        )
        if api_key is not None:
            self.api_key = api_key
        else:
            self.api_key = (
                os.environ.get("YODAW_LLM_API_KEY")
                or (os.environ.get(api_key_env) if api_key_env else "")
                or ""
            )

    def health(self):
        return {
            "style": self.style,
            "base_url": self.base_url,
            "model": self.model,
        }

    def available_models(self) -> dict:
        """List models/combos for 9Router-style providers.

        Only the 9Router style supports inventory today; other
        styles raise LLMError.
        """
        if self.style != "9router":
            raise LLMError(
                f"model inventory is only supported for the 9router "
                f"style (current style: {self.style})"
            )

        from app.llm import ninerouter

        try:
            return ninerouter.list_models(self.base_url, self.api_key)
        except ninerouter.NinerouterError as exc:
            raise LLMError(str(exc)) from exc

    def chat(self, system: str, user: str) -> str:
        """Chat with the primary style, then YODAW_LLM_FALLBACKS.

        Without YODAW_LLM_FALLBACKS this is exactly one attempt
        against the configured style (legacy behavior). With
        fallbacks, each style is tried once in order; every hop is
        recorded in the attempt log for evidence.
        """
        chain = [self.style]
        for style in fallback_styles():
            if style != self.style and style not in chain:
                chain.append(style)

        if len(chain) == 1:
            return self._chat_with_style(self.style, system, user)

        last_error: Optional[LLMError] = None
        for index, style in enumerate(chain):
            provider = (
                self if style == self.style else _sibling(style)
            )
            try:
                return provider._chat_with_style(style, system, user)
            except LLMError as exc:
                last_error = exc
                if index < len(chain) - 1:
                    _log_attempt(
                        {
                            "provider_fallback": True,
                            "from": style,
                            "to": chain[index + 1],
                            "error": str(exc)[:300],
                        }
                    )
        assert last_error is not None
        raise last_error

    def _chat_with_style(self, style: str, system: str, user: str) -> str:
        if style == "ollama":
            return self._ollama(system, user)

        if style == "openai":
            return self._openai(system, user)

        if style == "anthropic":
            return self._anthropic(system, user)

        if style == "9router":
            return self._ninerouter(system, user)

        raise LLMError(
            f"Unsupported YODAW_LLM_STYLE: {style} "
            f"(supported: {', '.join(SUPPORTED_STYLES)})"
        )

    def _chat_with_retry(
        self,
        url: str,
        payload: dict,
        headers: Optional[dict] = None,
        payloads: Optional[list[dict]] = None,
    ) -> dict:
        """
        Stage 8.6: bounded retries with exponential backoff and
        jitter for transient provider faults. Every attempt is
        logged for evidence; classification decides retryability.

        Failover: when ``payloads`` (a per-attempt model chain)
        is given, attempt N uses
        ``payloads[min(N - 1, len(payloads) - 1)]`` so each
        retryable failure fails over to the next route instead
        of hammering one dead route. Without ``payloads`` every
        attempt posts ``payload`` (legacy behavior).
        """
        max_retries, base_backoff = provider_retry_config()
        sequence = payloads if payloads else [payload]

        attempt = 0
        previous_model: Optional[str] = None

        while True:
            attempt += 1
            started = time.monotonic()
            used = sequence[min(attempt - 1, len(sequence) - 1)]
            model = (
                used.get("model") if isinstance(used, dict) else None
            )
            record = {"provider_attempt": attempt, "url": url}
            if model:
                record["model"] = model
            if previous_model and model and model != previous_model:
                _log_attempt(
                    {
                        "provider_model_fallback": True,
                        "from": previous_model,
                        "to": model,
                        "attempt": attempt,
                    }
                )
            previous_model = model or previous_model

            try:
                response = httpx.post(
                    url,
                    json=used,
                    headers=headers,
                    timeout=llm_timeout_seconds(),
                )
                response.raise_for_status()

                record["status"] = response.status_code
                record["duration_s"] = round(
                    time.monotonic() - started, 3
                )
                _log_attempt(record)

                return response.json()

            except Exception as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["duration_s"] = round(
                    time.monotonic() - started, 3
                )
                record["retryable"] = _is_retryable(exc)
                _log_attempt(record)

                if attempt > max_retries or not record["retryable"]:
                    raise LLMError(
                        f"provider request failed after "
                        f"{attempt} attempt(s): {exc}"
                    ) from exc

                delay = _backoff_delay(base_backoff, attempt)

                _log_attempt(
                    {
                        "provider_backoff": True,
                        "attempt": attempt,
                        "delay_s": round(delay, 3),
                    }
                )
                time.sleep(delay)

    def _ollama(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "keep_alive": llm_keep_alive(),
            "messages": [
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": user,
                },
            ],
        }

        data = self._chat_with_retry(
            f"{self.base_url}/api/chat",
            payload,
        )

        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMError(
                f"Ollama response malformed: {exc}"
            ) from exc

    def _openai(self, system: str, user: str) -> str:
        headers = {
            "Content-Type": "application/json",
        }

        if self.api_key:
            headers["Authorization"] = (
                f"Bearer {self.api_key}"
            )

        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": system,
                },
                {
                    "role": "user",
                    "content": user,
                },
            ],
        }

        data = self._chat_with_retry(
            f"{self.base_url}/v1/chat/completions",
            payload,
            headers=headers,
        )

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"OpenAI-compatible response malformed: {exc}"
            ) from exc

    def _ninerouter(self, system: str, user: str) -> str:
        from app.llm import ninerouter

        model = self.model
        if not model or model == "auto":
            try:
                model = ninerouter.resolve_model(
                    self.base_url, self.api_key, "auto"
                )
            except ninerouter.NinerouterError as exc:
                raise LLMError(str(exc)) from exc

        # Route failover chain: the primary model first, then each
        # YODAW_LLM_FALLBACK_MODELS route once. A route that times
        # out or 5xx-fails is abandoned for the next route on the
        # following attempt instead of being retried blindly.
        chain = [model]
        for name in fallback_models():
            if name != model and name not in chain:
                chain.append(name)

        url, payload, headers = ninerouter.build_chat_request(
            self.base_url, model, system, user, self.api_key
        )
        payloads = [dict(payload, model=name) for name in chain]

        data = self._chat_with_retry(
            url, payload, headers=headers, payloads=payloads
        )

        try:
            return ninerouter.parse_chat_response(data)
        except ninerouter.NinerouterError as exc:
            raise LLMError(str(exc)) from exc

    def _anthropic(self, system: str, user: str) -> str:
        if not self.api_key:
            raise LLMError(
                "anthropic provider requires an API key "
                "(YODAW_LLM_API_KEY)"
            )

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }

        payload = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": [
                {
                    "role": "user",
                    "content": user,
                },
            ],
        }

        data = self._chat_with_retry(
            f"{self.base_url}/v1/messages",
            payload,
            headers=headers,
        )

        try:
            parts = data.get("content") or []
            return "".join(
                part.get("text", "")
                for part in parts
                if part.get("type") == "text"
            )
        except (KeyError, TypeError, AttributeError) as exc:
            raise LLMError(
                f"Anthropic response malformed: {exc}"
            ) from exc
