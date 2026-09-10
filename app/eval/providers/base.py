"""Live evaluation provider adapters.

Provider-agnostic interface for the evaluation lab. Core evaluation
logic depends only on the types defined here, never on a vendor SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol


class LiveProviderError(RuntimeError):
    """Base class for provider failures.

    A provider failure must surface as BLOCKED_EXTERNAL, never as a
    benchmark task failure.
    """

    retryable = False

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 1,
        detail: Dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.detail = detail or {}


class ProviderUnavailable(LiveProviderError):
    """Network or connection failure. Retryable."""

    retryable = True


class ProviderTimeout(LiveProviderError):
    """Provider request timed out. Retryable."""

    retryable = True


class ProviderRateLimited(LiveProviderError):
    """Provider returned 429. Retryable."""

    retryable = True


class ProviderServerError(LiveProviderError):
    """Provider returned 5xx. Retryable."""

    retryable = True


class ProviderAuthError(LiveProviderError):
    """Authentication or authorization failure. Not retryable."""

    retryable = False


class ProviderMalformed(LiveProviderError):
    """Provider returned unusable output (bad JSON, empty content).

    This is a model-quality signal, not an outage. The executor maps
    it to a task failure, not BLOCKED_EXTERNAL.
    """

    retryable = False


class ProviderConfigError(LiveProviderError):
    """Invalid live evaluation configuration. Not retryable."""

    retryable = False


@dataclass
class ChatResult:
    """One completed chat interaction."""

    text: str
    latency_s: float
    attempts: int
    model: str
    usage: Dict[str, Any] = field(default_factory=dict)


OPENAI_COMPATIBLE_KIND = "openai-compatible"
OLLAMA_KIND = "ollama"
SUPPORTED_KINDS = (OPENAI_COMPATIBLE_KIND, OLLAMA_KIND)

DEFAULT_BASE_URLS = {
    OPENAI_COMPATIBLE_KIND: "http://127.0.0.1:4000",
    OLLAMA_KIND: "http://127.0.0.1:11434",
}

DEFAULT_API_KEY_ENV = "YODAW_EVAL_API_KEY"


@dataclass
class LiveProviderConfig:
    """Configuration for a live evaluation provider.

    The API key is never stored here; only the name of the
    environment variable it is read from. The key is read lazily on
    each request so rotation does not require a restart.
    """

    kind: str = OPENAI_COMPATIBLE_KIND
    model: str = ""
    base_url: str = ""
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout_s: float = 60.0
    max_retries: int = 2
    backoff_s: float = 0.5

    @classmethod
    def from_env(cls) -> "LiveProviderConfig":
        """Build configuration from YODAW_EVAL_* environment variables."""
        import os

        kind = os.environ.get("YODAW_EVAL_PROVIDER", OPENAI_COMPATIBLE_KIND)
        return cls(
            kind=kind,
            model=os.environ.get("YODAW_EVAL_MODEL", ""),
            base_url=os.environ.get(
                "YODAW_EVAL_BASE_URL", DEFAULT_BASE_URLS.get(kind, "")
            ),
            api_key_env=os.environ.get(
                "YODAW_EVAL_API_KEY_ENV", DEFAULT_API_KEY_ENV
            ),
            timeout_s=_parse_float("YODAW_EVAL_TIMEOUT_S", 60.0),
            max_retries=_parse_int("YODAW_EVAL_MAX_RETRIES", 2),
            backoff_s=_parse_float("YODAW_EVAL_BACKOFF_S", 0.5),
        )

    def api_key(self) -> str:
        """Read the API key from the environment. Never logged."""
        import os

        return os.environ.get(self.api_key_env, "")

    def api_key_is_set(self) -> bool:
        return bool(self.api_key())

    def validate(self) -> None:
        if self.kind not in SUPPORTED_KINDS:
            raise ProviderConfigError(
                f"unsupported provider kind: {self.kind!r} "
                f"(expected one of {list(SUPPORTED_KINDS)})"
            )
        if not self.model:
            raise ProviderConfigError("model is not configured")
        if not self.base_url:
            raise ProviderConfigError("base_url is not configured")
        if self.timeout_s <= 0:
            raise ProviderConfigError("timeout_s must be positive")
        if self.max_retries < 0:
            raise ProviderConfigError("max_retries must be >= 0")
        if self.backoff_s < 0:
            raise ProviderConfigError("backoff_s must be >= 0")

    def describe(self) -> Dict[str, Any]:
        """Log-safe summary. Contains no secrets."""
        return {
            "kind": self.kind,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "api_key_set": self.api_key_is_set(),
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "backoff_s": self.backoff_s,
        }

    def __repr__(self) -> str:
        return (
            f"LiveProviderConfig(kind={self.kind!r}, model={self.model!r}, "
            f"base_url={self.base_url!r}, api_key_env={self.api_key_env!r}, "
            f"api_key_set={self.api_key_is_set()!r}, "
            f"timeout_s={self.timeout_s!r}, max_retries={self.max_retries!r}, "
            f"backoff_s={self.backoff_s!r})"
        )


def _parse_float(name: str, default: float) -> float:
    import os

    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _parse_int(name: str, default: int) -> int:
    import os

    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class LiveProvider(Protocol):
    """Provider-agnostic chat interface used by live evaluation."""

    config: LiveProviderConfig

    def chat(self, system: str, user: str) -> ChatResult:
        ...
