from typing import Any, Optional
from pydantic import BaseModel, Field
from datetime import datetime, timezone
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LearningRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"lr_{uuid4().hex[:12]}")
    mission_id: Optional[str] = None
    goal: str
    worker: Optional[str] = None
    outcome: str
    strategy: Optional[str] = None
    root_cause: Optional[str] = None
    solution: Optional[str] = None
    retries: int = 0
    tools: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now_iso)
