from __future__ import annotations

import json
import os
import random
import threading
import time
from datetime import datetime, timezone

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
    failures, temporary HTTP 5xx and 429. Never retry
    deterministic errors: authentication, missing/unsupported
    model, bad request, malformed responses.
    """
    if isinstance(exc, httpx.TimeoutException):
        return True

    if isinstance(exc, httpx.ConnectError):
        return True

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status >= 500 or status == 429

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


class LocalLLMProvider:
    """
    Generic local LLM adapter.

    Supports:
      - Ollama native /api/chat
      - OpenAI-compatible /v1/chat/completions

    Configuration:
      YODAW_LLM_STYLE=ollama|openai
      YODAW_LLM_BASE_URL=http://127.0.0.1:11434
      YODAW_LLM_MODEL=<model-name>
      YODAW_LLM_API_KEY=<optional>
    """

    def __init__(self):
        self.style = os.environ.get(
            "YODAW_LLM_STYLE",
            "ollama",
        ).lower()

        self.base_url = os.environ.get(
            "YODAW_LLM_BASE_URL",
            "http://127.0.0.1:11434",
        ).rstrip("/")

        self.model = os.environ.get(
            "YODAW_LLM_MODEL",
            "",
        )

        self.api_key = os.environ.get(
            "YODAW_LLM_API_KEY",
            "",
        )

        if not self.model:
            raise LLMError(
                "YODAW_LLM_MODEL is not configured"
            )

    def health(self):
        return {
            "style": self.style,
            "base_url": self.base_url,
            "model": self.model,
        }

    def chat(self, system: str, user: str) -> str:
        if self.style == "ollama":
            return self._ollama(system, user)

        if self.style == "openai":
            return self._openai(system, user)

        raise LLMError(
            f"Unsupported YODAW_LLM_STYLE: {self.style}"
        )

    def _chat_with_retry(
        self,
        url: str,
        payload: dict,
        headers: dict | None = None,
    ) -> dict:
        """
        Stage 8.6: bounded retries with exponential backoff and
        jitter for transient provider faults. Every attempt is
        logged for evidence; classification decides retryability.
        """
        max_retries, base_backoff = provider_retry_config()

        attempt = 0

        while True:
            attempt += 1
            started = time.monotonic()
            record = {"provider_attempt": attempt, "url": url}

            try:
                response = httpx.post(
                    url,
                    json=payload,
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
