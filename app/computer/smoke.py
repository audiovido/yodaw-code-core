"""Manual smoke probe for computer control. Opt-in only, never runs in pytest."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safe manual smoke: screenshot + active window, "
        "optional harmless pointer nudge. Never clicks or types."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge this touches the real desktop.",
    )
    parser.add_argument(
        "--nudge-pointer",
        action="store_true",
        help="Also move the pointer 1px and back (harmless).",
    )
    args = parser.parse_args(argv)

    if not args.confirm:
        print("Refusing to run without --confirm (real desktop access).")
        return 2

    from app.computer.backends.macos import MacOSComputerBackend

    backend = MacOSComputerBackend()
    print(f"platform: {backend.platform()}")
    geometry = backend.screen_geometry()
    print(f"displays: {len(geometry.displays)}")
    for display in geometry.displays:
        print(
            f"  id={display.display_id} origin=({display.origin_x},"
            f"{display.origin_y}) size={display.width}x{display.height} "
            f"scale={display.scale_factor}"
        )
    try:
        active = backend.active_window()
        print(f"active_app: {active.app_name!r} window: {active.window_title!r}")
    except Exception as exc:
        print(f"active_window unavailable: {exc}")
        return 1
    try:
        shot = backend.screenshot()
        print(
            f"screenshot: {shot.width}x{shot.height} "
            f"scale={shot.scale_factor} bytes={len(shot.png_bytes)} "
            f"artifact={shot.artifact_id}"
        )
    except PermissionError as exc:
        print(f"screenshot BLOCKED_EXTERNAL: {exc}")
    except Exception as exc:
        print(f"screenshot failed: {exc}")
        return 1

    if args.nudge_pointer:
        x, y = backend.pointer_position()
        print(f"pointer at ({x}, {y}); nudging +1px and back")
        result = backend.move(x + 1, y)
        if result.ok:
            backend.move(x, y)
            print("nudge ok")
        else:
            print(f"nudge blocked: {result.error_class}: {result.error_message}")
    else:
        x, y = backend.pointer_position()
        print(f"pointer at ({x}, {y}) (not moved; pass --nudge-pointer)")
    print("No clicks or keystrokes were performed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
