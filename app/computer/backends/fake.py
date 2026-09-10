"""Hermetic fake backend: powers pytest, never touches the real desktop."""

from __future__ import annotations

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


def _png_stub(width: int = 16, height: int = 16) -> bytes:
    # Minimal valid PNG header + filler; decoders are not required
    # to do anything with it, but the bytes must be non-empty and
    # stable so metadata tests can assert on artifact ids.
    import struct
    import zlib

    def chunk(ctype: bytes, data: bytes) -> bytes:
        out = ctype + data
        return (
            struct.pack(">I", len(data))
            + out
            + struct.pack(">I", zlib.crc32(out) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(
        b"\x00" + bytes([i % 256, (i * 2) % 256, (i * 3) % 256] * width)
        for i in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeComputerBackend(ComputerBackend):
    """Deterministic in-memory desktop for tests and dry runs."""

    name = "fake"

    def __init__(
        self,
        geometry: ScreenGeometry | None = None,
        app_name: str = "FakeApp",
        fail_next: dict | None = None,
        deny_permission: str = "",
        screenshot_available: bool = True,
    ) -> None:
        if geometry is None:
            geometry = ScreenGeometry(
                displays=[
                    DisplayInfo(
                        display_id=0,
                        origin_x=0,
                        origin_y=0,
                        width=1440,
                        height=900,
                        scale_factor=2.0,
                    )
                ]
            )
        self._geometry = geometry
        self._pointer: tuple[float, float] = (100.0, 100.0)
        self._app = app_name
        self._window = WindowInfo(
            app_name=app_name,
            window_title=f"{app_name} Window",
            window_id=1,
            bounds=(0, 0, 800, 600),
            is_active=True,
        )
        self.calls: list[dict] = []
        self.typed_log: list[dict] = []
        self.fail_next: dict = dict(fail_next or {})
        self.deny_permission = deny_permission
        self.screenshot_available = screenshot_available
        self._shot_counter = 0
        self._last_pixels = b"pixels-0"

    def platform(self) -> str:
        return "fake"

    def screen_geometry(self) -> ScreenGeometry:
        return self._geometry

    def set_geometry(self, geometry: ScreenGeometry) -> None:
        self._geometry = geometry

    def _maybe_fail(self, op: str) -> BackendResult | None:
        if op in self.fail_next and self.fail_next[op] > 0:
            self.fail_next[op] -= 1
            return backend_result_fail("BACKEND_FAILURE", f"fake {op} failure")
        if op == "screenshot" and not self.screenshot_available:
            return backend_result_fail(
                "BACKEND_FAILURE", "screenshot unavailable"
            )
        if self.deny_permission and op in (
            "screenshot",
            "move",
            "click",
            "type",
            "key",
            "hotkey",
        ):
            return backend_result_fail(
                "PERMISSION_REQUIRED",
                f"permission {self.deny_permission} required",
                blocked_permission=self.deny_permission,
            )
        return None

    def screenshot(
        self,
        region: tuple[float, float, float, float] | None = None,
        active_window_only: bool = False,
    ) -> ScreenshotData:
        if self.deny_permission:
            raise PermissionError(
                f"permission {self.deny_permission} required"
            )
        failed = self._maybe_fail("screenshot")
        primary = self._geometry.primary
        if failed is not None:
            raise RuntimeError(failed.error_message)
        self._shot_counter += 1
        self._last_pixels = f"pixels-{self._shot_counter}".encode()
        width = int(primary.width if region is None else region[2])
        height = int(primary.height if region is None else region[3])
        return ScreenshotData(
            png_bytes=_png_stub(),
            width=width,
            height=height,
            scale_factor=primary.scale_factor,
            timestamp=time.time(),
            active_app=self._window.app_name,
            active_window=self._window.window_title,
            region=region,
            artifact_id=f"shot_{self._shot_counter}_{uuid.uuid4().hex[:8]}",
            backend_note="fake",
        )

    def last_pixels(self) -> bytes:
        return self._last_pixels

    def move(self, x: float, y: float, duration_ms: int = 0) -> BackendResult:
        failed = self._maybe_fail("move")
        if failed is not None:
            return failed
        self.calls.append({"op": "move", "x": x, "y": y})
        self._pointer = (float(x), float(y))
        return backend_result_ok(x=x, y=y)

    def click(
        self, x: float, y: float, button: str = "left", click_count: int = 1
    ) -> BackendResult:
        failed = self._maybe_fail("click")
        if failed is not None:
            return failed
        self.calls.append(
            {"op": "click", "x": x, "y": y, "button": button, "n": click_count}
        )
        self._pointer = (float(x), float(y))
        # Clicks observably change the pixel fingerprint so that
        # screenshot-before/after verification can detect them.
        self._shot_counter += 1
        self._last_pixels = f"pixels-{self._shot_counter}".encode()
        return backend_result_ok(x=x, y=y, button=button, count=click_count)

    def mouse_down(
        self, x: float, y: float, button: str = "left"
    ) -> BackendResult:
        failed = self._maybe_fail("mouse_down")
        if failed is not None:
            return failed
        self.calls.append({"op": "mouse_down", "x": x, "y": y})
        self._pointer = (float(x), float(y))
        return backend_result_ok(x=x, y=y)

    def mouse_up(
        self, x: float, y: float, button: str = "left"
    ) -> BackendResult:
        failed = self._maybe_fail("mouse_up")
        if failed is not None:
            return failed
        self.calls.append({"op": "mouse_up", "x": x, "y": y})
        return backend_result_ok(x=x, y=y)

    def drag(
        self,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        duration_ms: int = 300,
    ) -> BackendResult:
        failed = self._maybe_fail("drag")
        if failed is not None:
            return failed
        self.calls.append(
            {"op": "drag", "from": (from_x, from_y), "to": (to_x, to_y)}
        )
        self._pointer = (float(to_x), float(to_y))
        self._shot_counter += 1
        self._last_pixels = f"pixels-{self._shot_counter}".encode()
        return backend_result_ok(moved=True)

    def scroll(
        self, x: float, y: float, dx: int = 0, dy: int = 0
    ) -> BackendResult:
        failed = self._maybe_fail("scroll")
        if failed is not None:
            return failed
        self.calls.append({"op": "scroll", "x": x, "y": y, "dx": dx, "dy": dy})
        self._shot_counter += 1
        self._last_pixels = f"pixels-{self._shot_counter}".encode()
        return backend_result_ok(dx=dx, dy=dy)

    def type(
        self, text: str, interval_ms: int = 12, secret: bool = False
    ) -> BackendResult:
        failed = self._maybe_fail("type")
        if failed is not None:
            return failed
        # The fake never stores plaintext for secrets; it keeps a
        # length fingerprint so verification can still assert.
        self.typed_log.append(
            {"len": len(text), "secret": secret, "interval_ms": interval_ms}
        )
        self.calls.append({"op": "type", "secret": secret})
        return backend_result_ok(typed_chars=0 if secret else len(text))

    def key(self, key_name: str) -> BackendResult:
        failed = self._maybe_fail("key")
        if failed is not None:
            return failed
        self.calls.append({"op": "key", "key": key_name})
        return backend_result_ok(key=key_name)

    def hotkey(self, keys: list[str]) -> BackendResult:
        failed = self._maybe_fail("hotkey")
        if failed is not None:
            return failed
        self.calls.append({"op": "hotkey", "keys": list(keys)})
        return backend_result_ok(keys=list(keys))

    def focus_window(
        self, app_name: str = "", window_title: str = ""
    ) -> BackendResult:
        failed = self._maybe_fail("focus_window")
        if failed is not None:
            return failed
        if app_name:
            self._app = app_name
        title = window_title or f"{self._app} Window"
        self._window = WindowInfo(
            app_name=self._app,
            window_title=title,
            window_id=self._window.window_id,
            bounds=self._window.bounds,
            is_active=True,
        )
        self.calls.append({"op": "focus_window", "app": self._app})
        return backend_result_ok(app=self._app, title=title)

    def active_window(self) -> WindowInfo:
        return self._window

    def pointer_position(self) -> tuple[float, float]:
        return self._pointer
