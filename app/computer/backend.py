"""Backend abstraction: one interface, many platforms."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from app.computer.geometry import ScreenGeometry


@dataclass
class WindowInfo:
    app_name: str = ""
    window_title: str = ""
    window_id: int = 0
    bounds: tuple[float, float, float, float] = (0, 0, 0, 0)
    is_active: bool = False


@dataclass
class ScreenshotData:
    png_bytes: bytes = b""
    width: int = 0
    height: int = 0
    scale_factor: float = 1.0
    timestamp: float = 0.0
    active_app: str = ""
    active_window: str = ""
    region: Optional[tuple[float, float, float, float]] = None
    artifact_id: str = ""
    backend_note: str = ""


@dataclass
class BackendResult:
    ok: bool
    data: dict = field(default_factory=dict)
    error_class: str = "NONE"
    error_message: str = ""
    blocked_permission: str = ""


class BackendNotAvailable(Exception):
    """Raised when a platform backend cannot run here."""


class ComputerBackend(ABC):
    """Platform-agnostic control surface for desktop automation."""

    name: str = "base"

    @abstractmethod
    def platform(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def screen_geometry(self) -> ScreenGeometry:
        raise NotImplementedError

    @abstractmethod
    def screenshot(
        self,
        region: Optional[tuple[float, float, float, float]] = None,
        active_window_only: bool = False,
    ) -> ScreenshotData:
        raise NotImplementedError

    @abstractmethod
    def move(self, x: float, y: float, duration_ms: int = 0) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def click(
        self,
        x: float,
        y: float,
        button: str = "left",
        click_count: int = 1,
    ) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def mouse_down(self, x: float, y: float, button: str = "left") -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def mouse_up(self, x: float, y: float, button: str = "left") -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        duration_ms: int = 300,
    ) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def scroll(self, x: float, y: float, dx: int = 0, dy: int = 0) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def type(
        self, text: str, interval_ms: int = 12, secret: bool = False
    ) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def key(self, key_name: str) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def hotkey(self, keys: list[str]) -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def focus_window(self, app_name: str = "", window_title: str = "") -> BackendResult:
        raise NotImplementedError

    @abstractmethod
    def active_window(self) -> WindowInfo:
        raise NotImplementedError

    def pointer_position(self) -> tuple[float, float]:
        return (0.0, 0.0)


def backend_result_ok(**data: Any) -> BackendResult:
    return BackendResult(ok=True, data=dict(data))


def backend_result_fail(
    error_class: str, message: str, blocked_permission: str = ""
) -> BackendResult:
    return BackendResult(
        ok=False,
        error_class=error_class,
        error_message=message,
        blocked_permission=blocked_permission,
    )
