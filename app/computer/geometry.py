"""Shared screen-geometry and coordinate helpers for computer control."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class DisplayInfo:
    """One physical display in global virtual-screen coordinates."""

    display_id: int
    origin_x: float
    origin_y: float
    width: float
    height: float
    scale_factor: float = 1.0

    @property
    def max_x(self) -> float:
        return self.origin_x + self.width

    @property
    def max_y(self) -> float:
        return self.origin_y + self.height

    def contains(self, x: float, y: float) -> bool:
        return (
            self.origin_x <= x < self.max_x
            and self.origin_y <= y < self.max_y
        )


@dataclass
class ScreenGeometry:
    """Snapshot of the virtual-screen layout at a point in time."""

    displays: list[DisplayInfo] = field(default_factory=list)
    generation: int = 0

    def __post_init__(self) -> None:
        if not self.displays:
            self.displays = [
                DisplayInfo(
                    display_id=0,
                    origin_x=0,
                    origin_y=0,
                    width=1440,
                    height=900,
                    scale_factor=1.0,
                )
            ]

    @property
    def primary(self) -> DisplayInfo:
        return self.displays[0]

    def total_bounds(self) -> tuple[float, float, float, float]:
        xs = [d.origin_x for d in self.displays]
        ys = [d.origin_y for d in self.displays]
        xe = [d.max_x for d in self.displays]
        ye = [d.max_y for d in self.displays]
        return (min(xs), min(ys), max(xe), max(ye))

    def display_at(self, x: float, y: float) -> Optional[DisplayInfo]:
        for display in self.displays:
            if display.contains(x, y):
                return display
        return None

    def contains(self, x: float, y: float) -> bool:
        return self.display_at(x, y) is not None

    def same_layout(self, other: "ScreenGeometry") -> bool:
        if len(self.displays) != len(other.displays):
            return False
        for mine, theirs in zip(self.displays, other.displays):
            if (
                mine.origin_x != theirs.origin_x
                or mine.origin_y != theirs.origin_y
                or mine.width != theirs.width
                or mine.height != theirs.height
                or mine.scale_factor != theirs.scale_factor
            ):
                return False
        return True


class CoordinateError(ValueError):
    """A coordinate is outside the valid virtual screen."""


def resolve_point(
    geometry: ScreenGeometry,
    *,
    x: Optional[float] = None,
    y: Optional[float] = None,
    normalized_x: Optional[float] = None,
    normalized_y: Optional[float] = None,
) -> tuple[float, float]:
    """Resolve absolute or normalized coordinates to an absolute point.

    Normalized coordinates are in [0, 1] relative to the total
    virtual-screen bounds (which may start at negative offsets on
    multi-monitor layouts).
    """
    if normalized_x is not None or normalized_y is not None:
        if normalized_x is None or normalized_y is None:
            raise CoordinateError(
                "normalized_x and normalized_y must be provided together"
            )
        if not (0.0 <= normalized_x <= 1.0 and 0.0 <= normalized_y <= 1.0):
            raise CoordinateError(
                f"normalized coordinates out of range: "
                f"({normalized_x}, {normalized_y})"
            )
        min_x, min_y, max_x, max_y = geometry.total_bounds()
        return (
            min_x + normalized_x * (max_x - min_x),
            min_y + normalized_y * (max_y - min_y),
        )
    if x is None or y is None:
        raise CoordinateError("x and y must be provided together")
    return (float(x), float(y))


def validate_point(geometry: ScreenGeometry, x: float, y: float) -> DisplayInfo:
    """Return the display containing a point or raise CoordinateError."""
    display = geometry.display_at(float(x), float(y))
    if display is None:
        min_x, min_y, max_x, max_y = geometry.total_bounds()
        raise CoordinateError(
            f"point ({x}, {y}) is outside the virtual screen "
            f"[{min_x},{min_y},{max_x},{max_y}]"
        )
    return display


def validate_region(
    geometry: ScreenGeometry,
    x: float,
    y: float,
    width: float,
    height: float,
) -> None:
    """Ensure a capture region is sane and anchored on the screen."""
    if width <= 0 or height <= 0:
        raise CoordinateError(
            f"region must have positive size, got {width}x{height}"
        )
    validate_point(geometry, x, y)
    validate_point(geometry, x + width - 1, y + height - 1)


def to_physical(display: DisplayInfo, x: float, y: float) -> tuple[int, int]:
    """Convert logical points to backing-store pixels (Retina aware)."""
    px = round((x - display.origin_x) * display.scale_factor)
    py = round((y - display.origin_y) * display.scale_factor)
    return (px, py)
