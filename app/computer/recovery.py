"""Bounded recovery for ambiguous desktop states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RecoveryAction(str, Enum):
    REFOCUS_APP = "refocus_app"
    RECAPTURE = "recapture"
    RETRY_ONCE = "retry_once"
    ABORT = "abort"
    NONE = "none"


@dataclass
class RecoveryPlan:
    action: RecoveryAction
    reason: str
    retry_allowed: bool = False
    max_retries: int = 1


def plan_recovery(
    *,
    target_app_mismatch: bool = False,
    screen_changed: bool = False,
    no_observable_change: bool = False,
    permission_revoked: bool = False,
    screenshot_unavailable: bool = False,
    backend_error: bool = False,
    attempts_so_far: int = 0,
) -> RecoveryPlan:
    """Choose a single bounded recovery step; never loop indefinitely."""
    if permission_revoked or screenshot_unavailable:
        return RecoveryPlan(
            action=RecoveryAction.ABORT,
            reason="external condition requires operator action",
        )
    if target_app_mismatch:
        return RecoveryPlan(
            action=RecoveryAction.REFOCUS_APP,
            reason="target app is not focused",
            retry_allowed=attempts_so_far < 1,
        )
    if screen_changed:
        return RecoveryPlan(
            action=RecoveryAction.RECAPTURE,
            reason="screen geometry changed",
            retry_allowed=attempts_so_far < 1,
        )
    if no_observable_change:
        if attempts_so_far < 1:
            return RecoveryPlan(
                action=RecoveryAction.RETRY_ONCE,
                reason="click produced no observable change",
                retry_allowed=True,
            )
        return RecoveryPlan(
            action=RecoveryAction.ABORT,
            reason="state is ambiguous after retry",
        )
    if backend_error:
        if attempts_so_far < 1:
            return RecoveryPlan(
                action=RecoveryAction.RETRY_ONCE,
                reason="temporary OS automation error",
                retry_allowed=True,
            )
        return RecoveryPlan(
            action=RecoveryAction.ABORT,
            reason="backend failed twice",
        )
    return RecoveryPlan(action=RecoveryAction.NONE, reason="no recovery needed")
