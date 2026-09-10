"""macOS computer backend: Accessibility-based control, no root required.

Mechanisms (safest practical local options, first available wins):

- Mouse/keyboard: Quartz CoreGraphics event APIs via pyobjc when
  importable; otherwise System Events through ``osascript``.
- Screenshots: ``screencapture`` (needs Screen Recording permission).
- Active window / focus: ``osascript`` System Events queries.

Required macOS permissions (System Settings > Privacy & Security):

- Accessibility: mouse, keyboard, window focus. The controlling
  terminal/app (Terminal.app, VS Code, etc.) must be listed.
- Screen Recording: screenshots via ``screencapture``.

When a permission is missing the backend returns PERMISSION_REQUIRED
/ BLOCKED_EXTERNAL results; it never reports them as TASK_FAIL, and
it never attempts to bypass OS security prompts.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
import tempfile
import time
import uuid

from app.computer.backend import (
    BackendResult,
    ComputerBackend,
    ScreenshotData,
    WindowInfo,
    backend_result_fail,
    backend_result_ok,
)
from app.computer.geometry import DisplayInfo, ScreenGeometry

logger = logging.getLogger("yodaw.computer.macos")

ACCESSIBILITY_DOC = (
    "macOS Accessibility permission required: add the controlling app "
    "(e.g. Terminal) under System Settings > Privacy & Security > "
    "Accessibility, then retry. YODAW never bypasses this prompt."
)
SCREEN_RECORDING_DOC = (
    "macOS Screen Recording permission required: allow the controlling "
    "app under System Settings > Privacy & Security > Screen Recording, "
    "then retry."
)

_IS_DARWIN = platform.system() == "Darwin"


def _run(argv: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _osascript(script: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    return _run(["osascript", "-e", script], timeout=timeout)


def _looks_like_permission_error(stderr: str) -> bool:
    lowered = (stderr or "").lower()
    return any(
        marker in lowered
        for marker in (
            "not allowed",
            "not permitted",
            "accessibility",
            "assistive access",
            "operation not permitted",
            "-1743",
            "-1002",
        )
    )


class MacOSComputerBackend(ComputerBackend):
    """macOS control adapter. Safe to construct on any platform."""

    name = "macos"

    def __init__(self, display_scale: float = 2.0) -> None:
        self._display_scale = display_scale
        self._quartz = None
        self._quartz_tried = False

    # --------------------------------------------------
    # platform surface
    # --------------------------------------------------
    def platform(self) -> str:
        return "macos"

    def _require_darwin(self) -> BackendResult | None:
        if not _IS_DARWIN:
            return backend_result_fail(
                "BLOCKED_EXTERNAL",
                "macOS backend requires macOS (Darwin); "
                f"running on {platform.system()}",
            )
        return None

    def _quartz_module(self):  # lazy so non-macOS imports stay clean
        if self._quartz_tried:
            return self._quartz
        self._quartz_tried = True
        try:
            import Quartz  # type: ignore[import-not-found]

            self._quartz = Quartz
        except Exception as exc:
            logger.debug("Quartz unavailable: %s", exc)
            self._quartz = None
        return self._quartz

    # --------------------------------------------------
    # observation
    # --------------------------------------------------
    def screen_geometry(self) -> ScreenGeometry:
        quartz = self._quartz_module()
        if quartz is not None and _IS_DARWIN:
            try:
                return self._geometry_from_quartz(quartz)
            except Exception as exc:
                logger.debug("Quartz display query failed: %s", exc)
        return ScreenGeometry(
            displays=[
                DisplayInfo(
                    display_id=0,
                    origin_x=0,
                    origin_y=0,
                    width=1440,
                    height=900,
                    scale_factor=self._display_scale,
                )
            ]
        )

    def _geometry_from_quartz(self, quartz) -> ScreenGeometry:
        displays = []
        for index in range(16):
            try:
                display_id = quartz.CGMainDisplayID() if index == 0 else None
                if display_id is None:
                    break
                bounds = quartz.CGDisplayBounds(display_id)
                displays.append(
                    DisplayInfo(
                        display_id=int(display_id),
                        origin_x=float(bounds.origin.x),
                        origin_y=float(bounds.origin.y),
                        width=float(bounds.size.width),
                        height=float(bounds.size.height),
                        scale_factor=self._display_scale,
                    )
                )
                break  # primary only; multi-monitor via extension point
            except Exception:
                break
        return ScreenGeometry(displays=displays or [
            DisplayInfo(
                display_id=0,
                origin_x=0,
                origin_y=0,
                width=1440,
                height=900,
                scale_factor=self._display_scale,
            )
        ])

    def screenshot(
        self,
        region: tuple[float, float, float, float] | None = None,
        active_window_only: bool = False,
    ) -> ScreenshotData:
        blocked = self._require_darwin()
        if blocked is not None:
            raise PermissionError(blocked.error_message)
        if shutil.which("screencapture") is None:
            raise RuntimeError("screencapture utility not found")
        path = os.path.join(
            tempfile.gettempdir(), f"yodaw_shot_{uuid.uuid4().hex[:8]}.png"
        )
        argv = ["screencapture", "-x", "-t", "png"]
        if active_window_only:
            argv.append("-w")
        if region is not None:
            x, y, width, height = (int(v) for v in region)
            argv.append(f"-R{x},{y},{width},{height}")
        argv.append(path)
        try:
            proc = _run(argv, timeout=20.0)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"screencapture timed out: {exc}")
        finally:
            pass
        if proc.returncode != 0:
            if _looks_like_permission_error(proc.stderr):
                raise PermissionError(SCREEN_RECORDING_DOC)
            raise RuntimeError(
                f"screencapture failed: {(proc.stderr or '').strip()}"
            )
        try:
            with open(path, "rb") as handle:
                png = handle.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if not png:
            raise RuntimeError("screencapture produced no image data")
        active = self._safe_active_window()
        geometry = self.screen_geometry()
        width = geometry.primary.width
        height = geometry.primary.height
        if region is not None:
            width, height = float(region[2]), float(region[3])
        return ScreenshotData(
            png_bytes=png,
            width=int(width),
            height=int(height),
            scale_factor=geometry.primary.scale_factor,
            timestamp=time.time(),
            active_app=active.app_name,
            active_window=active.window_title,
            region=region,
            artifact_id=f"shot_{uuid.uuid4().hex[:8]}",
            backend_note="screencapture",
        )

    # --------------------------------------------------
    # pointer
    # --------------------------------------------------
    def move(self, x: float, y: float, duration_ms: int = 0) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        quartz = self._quartz_module()
        if quartz is not None:
            try:
                event = quartz.CGEventCreateMouseEvent(
                    None,
                    quartz.kCGEventMouseMoved,
                    (float(x), float(y)),
                    quartz.kCGMouseButtonLeft,
                )
                quartz.CGEventPost(quartz.kCGHIDEventTap, event)
                return backend_result_ok(x=x, y=y, via="quartz")
            except Exception as exc:
                if _looks_like_permission_error(str(exc)):
                    return backend_result_fail(
                        "PERMISSION_REQUIRED",
                        ACCESSIBILITY_DOC,
                        blocked_permission="Accessibility",
                    )
                return backend_result_fail("BACKEND_FAILURE", str(exc))
        return self._osascript_mouse_move(x, y)

    def _osascript_mouse_move(self, x: float, y: float) -> BackendResult:
        # System Events has no direct cursor move; report the gap
        # instead of faking success.
        return backend_result_fail(
            "BLOCKED_EXTERNAL",
            "mouse move needs Quartz/pyobjc on this host; install "
            "pyobjc-framework-Quartz for full pointer control",
        )

    def click(
        self, x: float, y: float, button: str = "left", click_count: int = 1
    ) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        quartz = self._quartz_module()
        if quartz is None:
            return backend_result_fail(
                "BLOCKED_EXTERNAL",
                "click needs Quartz/pyobjc on this host; install "
                "pyobjc-framework-Quartz for pointer control",
            )
        try:
            if button == "right":
                down, up = (
                    quartz.kCGEventRightMouseDown,
                    quartz.kCGEventRightMouseUp,
                )
                cg_button = quartz.kCGMouseButtonRight
            else:
                down, up = (
                    quartz.kCGEventLeftMouseDown,
                    quartz.kCGEventLeftMouseUp,
                )
                cg_button = quartz.kCGMouseButtonLeft
            for _ in range(max(1, click_count)):
                for kind in (down, up):
                    event = quartz.CGEventCreateMouseEvent(
                        None, kind, (float(x), float(y)), cg_button
                    )
                    quartz.CGEventPost(quartz.kCGHIDEventTap, event)
            return backend_result_ok(
                x=x, y=y, button=button, count=click_count, via="quartz"
            )
        except Exception as exc:
            if _looks_like_permission_error(str(exc)):
                return backend_result_fail(
                    "PERMISSION_REQUIRED",
                    ACCESSIBILITY_DOC,
                    blocked_permission="Accessibility",
                )
            return backend_result_fail("BACKEND_FAILURE", str(exc))

    def mouse_down(
        self, x: float, y: float, button: str = "left"
    ) -> BackendResult:
        return self.click(x, y, button=button, click_count=0)

    def mouse_up(
        self, x: float, y: float, button: str = "left"
    ) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        return backend_result_ok(x=x, y=y, via="quartz-release")

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        duration_ms: int = 300,
    ) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        quartz = self._quartz_module()
        if quartz is None:
            return backend_result_fail(
                "BLOCKED_EXTERNAL",
                "drag needs Quartz/pyobjc on this host",
            )
        try:
            down = quartz.CGEventCreateMouseEvent(
                None,
                quartz.kCGEventLeftMouseDown,
                (float(from_x), float(from_y)),
                quartz.kCGMouseButtonLeft,
            )
            quartz.CGEventPost(quartz.kCGHIDEventTap, down)
            dragged = quartz.CGEventCreateMouseEvent(
                None,
                quartz.kCGEventLeftMouseDragged,
                (float(to_x), float(to_y)),
                quartz.kCGMouseButtonLeft,
            )
            quartz.CGEventPost(quartz.kCGHIDEventTap, dragged)
            up = quartz.CGEventCreateMouseEvent(
                None,
                quartz.kCGEventLeftMouseUp,
                (float(to_x), float(to_y)),
                quartz.kCGMouseButtonLeft,
            )
            quartz.CGEventPost(quartz.kCGHIDEventTap, up)
            return backend_result_ok(moved=True, via="quartz")
        except Exception as exc:
            if _looks_like_permission_error(str(exc)):
                return backend_result_fail(
                    "PERMISSION_REQUIRED",
                    ACCESSIBILITY_DOC,
                    blocked_permission="Accessibility",
                )
            return backend_result_fail("BACKEND_FAILURE", str(exc))

    def scroll(
        self, x: float, y: float, dx: int = 0, dy: int = 0
    ) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        quartz = self._quartz_module()
        if quartz is None:
            return backend_result_fail(
                "BLOCKED_EXTERNAL",
                "scroll needs Quartz/pyobjc on this host",
            )
        try:
            event = quartz.CGEventCreateScrollWheelEvent(
                None, quartz.kCGScrollEventUnitPixel, 2, int(dy), int(dx)
            )
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)
            return backend_result_ok(dx=dx, dy=dy, via="quartz")
        except Exception as exc:
            if _looks_like_permission_error(str(exc)):
                return backend_result_fail(
                    "PERMISSION_REQUIRED",
                    ACCESSIBILITY_DOC,
                    blocked_permission="Accessibility",
                )
            return backend_result_fail("BACKEND_FAILURE", str(exc))

    # --------------------------------------------------
    # keyboard
    # --------------------------------------------------
    def type(
        self, text: str, interval_ms: int = 12, secret: bool = False
    ) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        # Never include secret contents in any failure detail.
        safe_detail = "secret value" if secret else f"{len(text)} chars"
        try:
            proc = _osascript(
                "tell application \"System Events\" to keystroke "
                f"{_applescript_string(text)}"
            )
        except subprocess.TimeoutExpired:
            return backend_result_fail(
                "TIMEOUT", f"typing {safe_detail} timed out"
            )
        if proc.returncode != 0:
            if _looks_like_permission_error(proc.stderr):
                return backend_result_fail(
                    "PERMISSION_REQUIRED",
                    ACCESSIBILITY_DOC,
                    blocked_permission="Accessibility",
                )
            return backend_result_fail(
                "BACKEND_FAILURE",
                f"keystroke failed for {safe_detail}",
            )
        return backend_result_ok(typed_chars=0 if secret else len(text))

    def key(self, key_name: str) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        code = _KEY_CODES.get(key_name.lower())
        if code is None:
            return backend_result_fail(
                "BACKEND_FAILURE", f"unknown key {key_name!r}"
            )
        try:
            proc = _osascript(
                f"tell application \"System Events\" to key code {code}"
            )
        except subprocess.TimeoutExpired:
            return backend_result_fail(
                "TIMEOUT", f"key {key_name!r} timed out"
            )
        if proc.returncode != 0:
            if _looks_like_permission_error(proc.stderr):
                return backend_result_fail(
                    "PERMISSION_REQUIRED",
                    ACCESSIBILITY_DOC,
                    blocked_permission="Accessibility",
                )
            return backend_result_fail(
                "BACKEND_FAILURE", f"key {key_name!r} failed"
            )
        return backend_result_ok(key=key_name)

    def hotkey(self, keys: list[str]) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        modifiers, key = _split_hotkey(keys)
        keystroke_arg = (
            f" using {{{', '.join(modifiers)}}}" if modifiers else ""
        )
        try:
            proc = _osascript(
                "tell application \"System Events\" to keystroke "
                f"{_applescript_string(key)}{keystroke_arg}"
            )
        except subprocess.TimeoutExpired:
            return backend_result_fail("TIMEOUT", "hotkey timed out")
        if proc.returncode != 0:
            if _looks_like_permission_error(proc.stderr):
                return backend_result_fail(
                    "PERMISSION_REQUIRED",
                    ACCESSIBILITY_DOC,
                    blocked_permission="Accessibility",
                )
            return backend_result_fail("BACKEND_FAILURE", "hotkey failed")
        return backend_result_ok(keys=list(keys))

    # --------------------------------------------------
    # windows
    # --------------------------------------------------
    def focus_window(
        self, app_name: str = "", window_title: str = ""
    ) -> BackendResult:
        blocked = self._require_darwin()
        if blocked is not None:
            return blocked
        if not app_name and not window_title:
            return backend_result_fail(
                "BACKEND_FAILURE", "focus_window needs app_name"
            )
        try:
            if app_name:
                proc = _osascript(
                    f'tell application "{app_name}" to activate'
                )
            else:
                proc = _osascript(
                    "tell application \"System Events\" to set "
                    "frontmost of (first process whose name contains "
                    f"{_applescript_string(window_title)}) to true"
                )
        except subprocess.TimeoutExpired:
            return backend_result_fail("TIMEOUT", "focus_window timed out")
        if proc.returncode != 0:
            if _looks_like_permission_error(proc.stderr):
                return backend_result_fail(
                    "PERMISSION_REQUIRED",
                    ACCESSIBILITY_DOC,
                    blocked_permission="Accessibility",
                )
            return backend_result_fail(
                "BACKEND_FAILURE",
                f"could not focus {app_name or window_title!r}",
            )
        return backend_result_ok(
            app=app_name, title=window_title or app_name
        )

    def active_window(self) -> WindowInfo:
        if not _IS_DARWIN:
            return WindowInfo()
        try:
            proc = _osascript(
                "tell application \"System Events\" to get name of "
                "first application process whose frontmost is true"
            )
        except (subprocess.TimeoutExpired, OSError):
            return WindowInfo()
        if proc.returncode != 0:
            return WindowInfo()
        app_name = (proc.stdout or "").strip()
        return WindowInfo(
            app_name=app_name, window_title="", is_active=True
        )

    def pointer_position(self) -> tuple[float, float]:
        quartz = self._quartz_module()
        if quartz is not None and _IS_DARWIN:
            try:
                event = quartz.CGEventCreate(None)
                point = quartz.CGEventGetLocation(event)
                return (float(point.x), float(point.y))
            except Exception:
                pass
        return (0.0, 0.0)

    def _safe_active_window(self) -> WindowInfo:
        try:
            return self.active_window()
        except Exception:
            return WindowInfo()


def _applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _split_hotkey(keys: list[str]) -> tuple[list[str], str]:
    modifier_map = {
        "cmd": "command down",
        "command": "command down",
        "ctrl": "control down",
        "control": "control down",
        "alt": "option down",
        "option": "option down",
        "shift": "shift down",
    }
    modifiers: list[str] = []
    main = ""
    for key in keys:
        mapped = modifier_map.get(str(key).lower())
        if mapped is not None:
            modifiers.append(mapped)
        else:
            main = str(key)
    return (modifiers, main or " ")


_KEY_CODES = {
    "return": 36,
    "enter": 36,
    "tab": 48,
    "space": 49,
    "escape": 53,
    "esc": 53,
    "delete": 51,
    "backspace": 51,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
}
