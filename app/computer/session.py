"""Computer session model: lifecycle of one desktop-control episode."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from app.computer.geometry import ScreenGeometry


class SessionStatus(str, Enum):
    READY = "READY"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


@dataclass
class ComputerSession:
    session_id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:12]}")
    platform: str = "unknown"
    geometry: ScreenGeometry = field(default_factory=ScreenGeometry)
    active_app: str = ""
    active_window: str = ""
    started_at: float = field(default_factory=time.time)
    last_action_id: str | None = None
    last_action_at: float | None = None
    status: SessionStatus = SessionStatus.READY
    dry_run: bool = False
    history: list[str] = field(default_factory=list)

    def touch(self, action_id: str) -> None:
        self.last_action_id = action_id
        self.last_action_at = time.time()
        self.history.append(action_id)
        if self.status == SessionStatus.READY:
            self.status = SessionStatus.ACTIVE

    def describe(self) -> dict:
        return {
            "session_id": self.session_id,
            "platform": self.platform,
            "status": self.status.value,
            "active_app": self.active_app,
            "active_window": self.active_window,
            "started_at": self.started_at,
            "last_action_id": self.last_action_id,
            "last_action_at": self.last_action_at,
            "action_count": len(self.history),
            "dry_run": self.dry_run,
            "displays": [
                {
                    "display_id": d.display_id,
                    "origin": [d.origin_x, d.origin_y],
                    "size": [d.width, d.height],
                    "scale_factor": d.scale_factor,
                }
                for d in self.geometry.displays
            ],
        }
