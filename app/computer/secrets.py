"""Secret-safe value abstraction for typed credentials."""

from __future__ import annotations

from dataclasses import dataclass, field

REDACTED = "[REDACTED]"


@dataclass(frozen=True)
class SecretValue:
    """A credential whose contents must never appear in logs/evidence.

    The plaintext is held in memory only for the backend to type.
    Every logging, repr, and evidence path must go through
    describe()/sanitize(), never str/repr of the plaintext.
    """

    secret_id: str
    length: int = 0
    _plaintext: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_plaintext(cls, secret_id: str, plaintext: str) -> "SecretValue":
        return cls(
            secret_id=secret_id,
            length=len(plaintext),
            _plaintext=plaintext,
        )

    def reveal(self) -> str:
        """Return the plaintext for the backend only. Never log this."""
        return self._plaintext

    def describe(self) -> str:
        return f"<secret:{self.secret_id} len={self.length}>"

    def __repr__(self) -> str:  # pragma: no cover - safety net
        return self.describe()


def sanitize(value: object) -> object:
    """Replace SecretValue instances with a safe descriptor."""
    if isinstance(value, SecretValue):
        return value.describe()
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        cleaned = [sanitize(item) for item in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    return value


def sanitize_message(message: str, secrets: list[str]) -> str:
    """Scrub known plaintext secrets out of an error message."""
    cleaned = message or ""
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, REDACTED)
    return cleaned
