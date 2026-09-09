from typing import Any
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LearningRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"lr_{uuid4().hex[:12]}")
    mission_id: str | None = None
    goal: str
    worker: str | None = None
    outcome: str
    strategy: str | None = None
    root_cause: str | None = None
    solution: str | None = None
    retries: int = 0
    tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)
