from enum import Enum
from typing import Any
from uuid import uuid4
from datetime import datetime, timezone

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionStatus(str, Enum):
    queued = "QUEUED"
    running = "RUNNING"
    passed = "PASS"
    failed = "FAIL"
    blocked = "BLOCKED"


class MissionCreate(BaseModel):
    goal: str
    capability: str = "code"
    metadata: dict[str, Any] = Field(default_factory=dict)


class Mission(BaseModel):
    id: str = Field(default_factory=lambda: f"m_{uuid4().hex[:12]}")
    goal: str
    capability: str
    status: MissionStatus = MissionStatus.queued
    worker: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
