"""Structured computer-use actions, results, and classifications."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class ActionType(str, Enum):
    MOVE_MOUSE = "move_mouse"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    MOUSE_DOWN = "mouse_down"
    MOUSE_UP = "mouse_up"
    DRAG = "drag"
    SCROLL = "scroll"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    HOTKEY = "hotkey"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    FOCUS_WINDOW = "focus_window"
    ACTIVATE_APPLICATION = "activate_application"
    GET_ACTIVE_WINDOW = "get_active_window"


class VerificationState(str, Enum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"


class ErrorClass(str, Enum):
    NONE = "NONE"
    VALIDATION = "VALIDATION"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
    BACKEND_FAILURE = "BACKEND_FAILURE"
    TIMEOUT = "TIMEOUT"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    AMBIGUOUS_STATE = "AMBIGUOUS_STATE"


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


_DEFAULT_RISK: dict[ActionType, RiskLevel] = {
    ActionType.MOVE_MOUSE: RiskLevel.LOW,
    ActionType.SCROLL: RiskLevel.LOW,
    ActionType.SCREENSHOT: RiskLevel.LOW,
    ActionType.GET_ACTIVE_WINDOW: RiskLevel.LOW,
    ActionType.FOCUS_WINDOW: RiskLevel.LOW,
    ActionType.CLICK: RiskLevel.MEDIUM,
    ActionType.DOUBLE_CLICK: RiskLevel.MEDIUM,
    ActionType.RIGHT_CLICK: RiskLevel.MEDIUM,
    ActionType.MOUSE_DOWN: RiskLevel.MEDIUM,
    ActionType.MOUSE_UP: RiskLevel.MEDIUM,
    ActionType.DRAG: RiskLevel.MEDIUM,
    ActionType.TYPE_TEXT: RiskLevel.MEDIUM,
    ActionType.PRESS_KEY: RiskLevel.MEDIUM,
    ActionType.HOTKEY: RiskLevel.MEDIUM,
    ActionType.WAIT: RiskLevel.LOW,
    ActionType.ACTIVATE_APPLICATION: RiskLevel.MEDIUM,
}


def default_risk(action_type: ActionType) -> RiskLevel:
    return _DEFAULT_RISK.get(action_type, RiskLevel.MEDIUM)


def now_ts() -> float:
    return time.time()


def new_action_id() -> str:
    return f"act_{uuid.uuid4().hex[:12]}"


@dataclass
class ActionRequest:
    """Validated caller intent before execution."""

    action_type: ActionType
    params: dict = field(default_factory=dict)
    action_id: str = field(default_factory=new_action_id)
    risk: RiskLevel | None = None
    requires_approval: bool = False
    timeout_seconds: float = 30.0
    verify: str = "default"
    dry_run: bool = False

    def __post_init__(self) -> None:
        if self.risk is None:
            self.risk = default_risk(self.action_type)
        if self.risk == RiskLevel.HIGH:
            self.requires_approval = True


@dataclass
class ActionResult:
    """Complete record of one executed (or blocked) action."""

    action_id: str
    action_type: ActionType
    timestamp: float
    requested_params: dict
    execution_result: dict
    duration_ms: float
    verification: VerificationState
    evidence_ref: str | None
    error_class: ErrorClass
    error_message: str | None = None
    risk: RiskLevel = RiskLevel.MEDIUM
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.error_class == ErrorClass.NONE
            and self.verification == VerificationState.VERIFIED
        )
