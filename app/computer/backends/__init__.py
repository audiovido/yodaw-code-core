"""Backend package: platform-specific control adapters."""

from app.computer.backends.fake import FakeComputerBackend

try:
    from app.computer.backends.macos import MacOSComputerBackend
except Exception:  # pragma: no cover - import-time platform guard
    MacOSComputerBackend = None  # type: ignore[assignment,misc]

__all__ = ["FakeComputerBackend", "MacOSComputerBackend"]
