"""
Stage 9.3: secret redaction for audit and evidence payloads.

Any value whose key looks secret-bearing is replaced by a fixed
marker. Redaction is recursive and applies to nested dicts and
lists, so a leaked key anywhere in a payload cannot reach the
audit trail, mission evidence, or events.
"""

from __future__ import annotations

REDACTED = "[REDACTED]"

_SENSITIVE_MARKERS = (
    "api_key",
    "apikey",
    "api-key",
    "authorization",
    "bearer",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "private_key",
    "privatekey",
)


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")

    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def redact(value):
    """
    Return a deep copy of value with secret-looking values replaced.

    Dict keys are checked recursively; list items are walked so a
    nested object inside a list is redacted too. Non-dict/list
    scalars pass through unchanged.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and is_sensitive_key(key)
                else redact(item)
            )
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [redact(item) for item in value]

    return value
