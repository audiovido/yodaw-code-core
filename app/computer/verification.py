"""Post-action verification: never trust the OS return code alone."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.computer.actions import ActionRequest, VerificationState
from app.computer.backend import ComputerBackend

VerifyPredicate = Callable[[dict], bool]


@dataclass
class Observation:
    pixels_before: bytes = b""
    pixels_after: bytes = b""
    app_before: str = ""
    app_after: str = ""
    window_before: str = ""
    window_after: str = ""


def pixels_changed(obs: Observation) -> bool:
    return bool(obs.pixels_before) and obs.pixels_before != obs.pixels_after


def app_changed(obs: Observation) -> bool:
    return bool(obs.app_before) and obs.app_before != obs.app_after


def expected_app_active(obs: Observation, expected: str) -> bool:
    return bool(expected) and obs.app_after == expected


def verify_action(
    request: ActionRequest,
    backend: ComputerBackend,
    obs: Observation,
    execution_ok: bool,
    predicate: VerifyPredicate | None = None,
    predicate_context: dict[str, Any] | None = None,
) -> VerificationState:
    """Decide VERIFIED / UNVERIFIED / FAILED for one executed action."""
    if not execution_ok:
        return VerificationState.FAILED
    mode = (request.verify or "default").lower()
    if mode == "none":
        return VerificationState.VERIFIED
    if predicate is not None:
        try:
            passed = bool(predicate(dict(predicate_context or {})))
        except Exception:
            return VerificationState.FAILED
        return (
            VerificationState.VERIFIED
            if passed
            else VerificationState.FAILED
        )
    action = request.action_type.value
    if action in ("move_mouse", "wait", "get_active_window", "screenshot"):
        return VerificationState.VERIFIED
    if action in ("focus_window", "activate_application"):
        expected = str(
            request.params.get("app_name")
            or request.params.get("target")
            or ""
        )
        if expected and expected_app_active(obs, expected):
            return VerificationState.VERIFIED
        if app_changed(obs):
            return VerificationState.VERIFIED
        return VerificationState.UNVERIFIED
    if action in (
        "click",
        "double_click",
        "right_click",
        "mouse_down",
        "mouse_up",
        "drag",
        "scroll",
        "type_text",
        "press_key",
        "hotkey",
    ):
        if pixels_changed(obs):
            return VerificationState.VERIFIED
        if app_changed(obs):
            return VerificationState.VERIFIED
        return VerificationState.UNVERIFIED
    return VerificationState.UNVERIFIED
