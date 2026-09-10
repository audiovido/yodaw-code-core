"""HTTP live providers: OpenAI-compatible and Ollama-compatible.

Both providers speak plain HTTP with no vendor SDK. Core evaluation
logic depends only on the LiveProvider protocol, never on this module.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import httpx

from app.eval.providers.base import (
    ChatResult,
    LiveProviderConfig,
    LiveProviderError,
    OLLAMA_KIND,
    OPENAI_COMPATIBLE_KIND,
    ProviderAuthError,
    ProviderConfigError,
    ProviderMalformed,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTimeout,
    ProviderUnavailable,
)


class HttpChatProvider:
    """HTTP chat provider with timeouts and bounded retries.

    Compatible with the legacy coder provider interface: chat() returns
    the raw text so it can be injected into generate_edit_plan().
    Use chat_with_result() when latency/usage metadata is needed.
    """

    def __init__(
        self,
        config: LiveProviderConfig,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        config.validate()
        self.config = config
        self._transport = transport
        self.attempts_log: List[Dict[str, Any]] = []
        self.last_error: Optional[LiveProviderError] = None
        self.last_result: Optional[ChatResult] = None

    @classmethod
    def from_args(
        cls,
        kind: str,
        model: str,
        base_url: str,
        api_key_env: str = "YODAW_EVAL_API_KEY",
        timeout_s: float = 60.0,
        max_retries: int = 2,
        backoff_s: float = 0.5,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> "HttpChatProvider":
        return cls(
            LiveProviderConfig(
                kind=kind,
                model=model,
                base_url=base_url.rstrip("/"),
                api_key_env=api_key_env,
                timeout_s=timeout_s,
                max_retries=max_retries,
                backoff_s=backoff_s,
            ),
            transport=transport,
        )

    def chat(self, system: str, user: str) -> str:
        return self.chat_with_result(system, user).text

    def chat_with_result(self, system: str, user: str) -> ChatResult:
        self.last_error = None
        self.last_result = None
        started = time.monotonic()
        attempts = 0
        url, payload, headers = self._build_request(system, user)
        max_attempts = self.config.max_retries + 1
        last_exc: Optional[LiveProviderError] = None

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            try:
                data = self._post(url, payload, headers)
                text, usage = self._parse_response(data)
                result = ChatResult(
                    text=text,
                    latency_s=time.monotonic() - started,
                    attempts=attempts,
                    model=self.config.model,
                    usage=usage,
                )
                self.attempts_log.append(
                    {"attempt": attempt, "ok": True, "url": url}
                )
                self.last_result = result
                return result
            except LiveProviderError as exc:
                exc.attempts = attempt
                last_exc = exc
                self.attempts_log.append(
                    {
                        "attempt": attempt,
                        "ok": False,
                        "error": type(exc).__name__,
                    }
                )
                if not exc.retryable or attempt >= max_attempts:
                    self.last_error = exc
                    raise
                self._sleep(attempt)

        self.last_error = last_exc
        raise last_exc  # pragma: no cover

    def _build_request(self, system: str, user: str):
        base = self.config.base_url.rstrip("/")
        key = self.config.api_key()
        if self.config.kind == OPENAI_COMPATIBLE_KIND:
            url = f"{base}/v1/chat/completions"
            payload = {
                "model": self.config.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            return url, payload, headers
        if self.config.kind == OLLAMA_KIND:
            url = f"{base}/api/chat"
            payload = {
                "model": self.config.model,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            return url, payload, {"Content-Type": "application/json"}
        raise ProviderConfigError(
            f"unsupported provider kind: {self.config.kind!r}"
        )

    def _post(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]):
        try:
            with httpx.Client(
                timeout=self.config.timeout_s, transport=self._transport
            ) as client:
                response = client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"provider request timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            raise ProviderUnavailable(f"provider unreachable: {exc}") from exc
        except httpx.NetworkError as exc:
            raise ProviderUnavailable(f"provider network error: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise self._classify_status(exc) from exc
        except LiveProviderError:
            raise
        except ValueError as exc:
            raise ProviderMalformed(
                f"provider returned invalid JSON: {exc}"
            ) from exc
        except Exception as exc:
            raise LiveProviderError(f"provider request failed: {exc}") from exc

    def _classify_status(self, exc: httpx.HTTPStatusError) -> LiveProviderError:
        status = exc.response.status_code
        if status in (401, 403):
            return ProviderAuthError(
                f"provider authentication failed (status {status})"
            )
        if status == 429:
            return ProviderRateLimited(
                f"provider rate limited (status 429)"
            )
        if 500 <= status <= 599:
            return ProviderServerError(
                f"provider server error (status {status})"
            )
        return LiveProviderError(f"provider request failed (status {status})")

    def _parse_response(self, data: Any):
        try:
            if self.config.kind == OPENAI_COMPATIBLE_KIND:
                choices = data["choices"]
                text = choices[0]["message"]["content"]
                usage = data.get("usage", {}) or {}
            else:
                text = data["message"]["content"]
                usage = {
                    k: data[k]
                    for k in (
                        "prompt_eval_count",
                        "eval_count",
                        "total_duration",
                    )
                    if k in data
                }
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ProviderMalformed(
                f"provider response malformed: {exc}"
            ) from exc
        if not isinstance(text, str) or not text.strip():
            raise ProviderMalformed("provider returned empty content")
        if not isinstance(usage, dict):
            usage = {}
        return text, usage

    def _sleep(self, attempt: int) -> None:
        delay = self.config.backoff_s * (2 ** (attempt - 1))
        if delay > 0:
            time.sleep(delay)
