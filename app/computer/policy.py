"""Risk classification and approval gates for computer actions."""

from __future__ import annotations

import re

from app.computer.actions import ActionRequest, ActionType, RiskLevel


class ApprovalRequired(Exception):
    """A HIGH-risk action was requested without approval."""


_SENSITIVE_PATTERNS = (
    re.compile(r"passw", re.IGNORECASE),
    re.compile(r"secur", re.IGNORECASE),
    re.compile(r"api.?key", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"payment|billing|checkout|purchase", re.IGNORECASE),
    re.compile(r"delete|uninstall|erase|format", re.IGNORECASE),
    re.compile(r"system (settings|preferences)", re.IGNORECASE),
    re.compile(r"permission|privacy", re.IGNORECASE),
    re.compile(r"install", re.IGNORECASE),
)

_DESTRUCTIVE_KEYS = frozenset({"delete", "backspace"})
_SYSTEM_HOTKEYS = frozenset(
    {
        "cmd+q",
        "cmd+shift+q",
        "ctrl+alt+delete",
        "cmd+alt+esc",
        "cmd+,",
    }
)


def _text(params: dict) -> str:
    parts = []
    for key in ("text", "target", "app_name", "window_title", "field"):
        value = params.get(key)
        if isinstance(value, str):
            parts.append(value)
    keys = params.get("keys")
    if isinstance(keys, list):
        parts.extend(str(k) for k in keys if isinstance(k, str))
    single = params.get("key")
    if isinstance(single, str):
        parts.append(single)
    return " ".join(parts)


def classify(request: ActionRequest) -> RiskLevel:
    """Escalate MEDIUM requests to HIGH when they look destructive."""
    base = request.risk
    text = _text(request.params)
    if request.action_type in (
        ActionType.TYPE_TEXT,
        ActionType.PRESS_KEY,
        ActionType.HOTKEY,
        ActionType.CLICK,
        ActionType.DOUBLE_CLICK,
        ActionType.RIGHT_CLICK,
    ):
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(text):
                return RiskLevel.HIGH
    if request.action_type == ActionType.PRESS_KEY:
        key = str(request.params.get("key", "")).lower()
        if key in _DESTRUCTIVE_KEYS and "delete" in text.lower():
            return RiskLevel.HIGH
    if request.action_type == ActionType.HOTKEY:
        keys = request.params.get("keys", [])
        combo = "+".join(str(k).lower() for k in keys if isinstance(k, str))
        if combo in _SYSTEM_HOTKEYS:
            return RiskLevel.HIGH
    return base


def check_approval(request: ActionRequest, approved: bool = False) -> RiskLevel:
    """Apply classification and enforce the HIGH-risk approval gate."""
    level = classify(request)
    request.risk = level
    if level == RiskLevel.HIGH:
        request.requires_approval = True
        if not approved and not request.params.get("approved", False):
            raise ApprovalRequired(
                f"HIGH-risk action {request.action_type.value} "
                f"(id={request.action_id}) requires explicit approval"
            )
    return level
