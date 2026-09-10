"""Human-like, reliability-first action timing configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimingConfig:
    typing_interval_ms: int = 12
    mouse_move_duration_ms: int = 120
    click_delay_ms: int = 60
    drag_duration_ms: int = 300
    action_settle_ms: int = 150
    double_click_interval_ms: int = 120

    def __post_init__(self) -> None:
        for name in (
            "typing_interval_ms",
            "mouse_move_duration_ms",
            "click_delay_ms",
            "drag_duration_ms",
            "action_settle_ms",
            "double_click_interval_ms",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")


DEFAULT_TIMING = TimingConfig()
