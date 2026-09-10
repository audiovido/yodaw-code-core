"""Secret redaction for CLI display and stored session text."""

from __future__ import annotations

import os
import re

REDACTED = "[REDACTED]"

# Value patterns for common secret formats.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"xox[bpas]-[A-Za-z0-9-]{8,}"),
    re.compile(r"sk-(?:live|test)-[A-Za-z0-9]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9-]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)

# Key=value assignments where the value must be hidden.
_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(api[_-]?key|apikey|auth[_-]?token|access[_-]?token|secret|password|passwd|pwd)\b\s*[:=]\s*['\"]?([^\s'\";,]+)['\"]?"),
    re.compile(r"(?i)\b(Bearer)\b\s+([A-Za-z0-9\-._~+/=]{8,})"),
)

# Environment variables whose values are secrets; never printed raw.
_SECRET_ENV_VARS = (
    "YODAW_API_KEY",
    "YODAW_LLM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


def _secret_values() -> list[str]:
    values = []
    for name in _SECRET_ENV_VARS:
        raw = os.environ.get(name, "")
        if raw and len(raw) >= 4:
            values.append(raw)
    # Longer values first so substrings do not partially mask.
    values.sort(key=len, reverse=True)
    return values


def redact_text(text: str) -> str:
    """Replace secret values and key=value secrets with [REDACTED]."""
    if not text:
        return text
    redacted = text
    for value in _secret_values():
        redacted = redacted.replace(value, REDACTED)
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    for pattern in _KEY_PATTERNS:
        redacted = pattern.sub(lambda m: m.group(0).replace(m.group(2), REDACTED), redacted)
    return redacted


def redact_mapping(data: dict) -> dict:
    """Return a copy of a dict with secret-looking keys masked."""
    out = {}
    for key, value in data.items():
        lowered = str(key).lower()
        if any(token in lowered for token in ("api_key", "apikey", "token", "secret", "password", "passwd", "authorization")):
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = redact_text(value)
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [redact_mapping(v) if isinstance(v, dict) else (redact_text(v) if isinstance(v, str) else v) for v in value]
        else:
            out[key] = value
    return out
