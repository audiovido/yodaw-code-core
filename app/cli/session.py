"""Agent session model with safe local persistence for resume."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.cli.redact import redact_mapping

SESSION_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sessions_dir() -> Path:
    """Local session store; honored env override exists for tests."""
    import os
    override = os.environ.get("YODAW_SESSIONS_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".yodaw" / "sessions"
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass
class TaskRecord:
    """One natural-language task executed inside a session."""
    task_id: str = field(default_factory=lambda: f"task_{uuid4().hex[:8]}")
    goal: str = ""
    status: str = "pending"
    started_at: str = field(default_factory=_now_iso)
    finished_at: str | None = None
    plan_id: str | None = None
    plan_steps: int = 0
    evidence_refs: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Session:
    """Persistent REPL state: identity, conversation, and task ledger."""
    session_id: str = field(default_factory=lambda: f"sess_{uuid4().hex[:8]}")
    repo: str | None = None
    branch: str | None = None
    started_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    current_task_id: str | None = None
    tasks: list[TaskRecord] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    provider: str | None = None
    approval_mode: str = "standard"
    plan_progress: dict[str, Any] = field(default_factory=dict)
    version: int = SESSION_VERSION

    def touch(self) -> None:
        self.updated_at = _now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "repo": self.repo,
            "branch": self.branch,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "current_task_id": self.current_task_id,
            "tasks": [asdict(task) for task in self.tasks],
            "history": self.history,
            "evidence": self.evidence,
            "model": self.model,
            "provider": self.provider,
            "approval_mode": self.approval_mode,
            "plan_progress": self.plan_progress,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        tasks = [TaskRecord(**task) for task in data.get("tasks", [])]
        return cls(
            session_id=data.get("session_id", f"sess_{uuid4().hex[:8]}"),
            repo=data.get("repo"),
            branch=data.get("branch"),
            started_at=data.get("started_at", _now_iso()),
            updated_at=data.get("updated_at", _now_iso()),
            current_task_id=data.get("current_task_id"),
            tasks=tasks,
            history=list(data.get("history", [])),
            evidence=list(data.get("evidence", [])),
            model=data.get("model"),
            provider=data.get("provider"),
            approval_mode=data.get("approval_mode", "standard"),
            plan_progress=dict(data.get("plan_progress", {})),
            version=data.get("version", SESSION_VERSION),
        )


def session_path(session_id: str, directory: Path | None = None) -> Path:
    return (directory or sessions_dir()) / f"{session_id}.json"


def save_session(session: Session, directory: Path | None = None) -> Path:
    """Persist a session as redacted JSON; secrets never touch disk."""
    session.touch()
    payload = redact_mapping(session.to_dict())
    path = session_path(session.session_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return path


def load_session(session_id: str, directory: Path | None = None) -> Session | None:
    """Load a persisted session, or None when it is missing/corrupt."""
    path = session_path(session_id, directory)
    try:
        return Session.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def list_sessions(directory: Path | None = None) -> list[dict[str, Any]]:
    """Summaries of persisted sessions, newest first."""
    base = directory or sessions_dir()
    summaries = []
    try:
        files = sorted(base.glob("sess_*.json"))
    except OSError:
        return []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        summaries.append(
            {
                "session_id": data.get("session_id", path.stem),
                "repo": data.get("repo"),
                "branch": data.get("branch"),
                "started_at": data.get("started_at"),
                "updated_at": data.get("updated_at"),
                "tasks": len(data.get("tasks", [])),
                "history": len(data.get("history", [])),
            }
        )
    summaries.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return summaries


def latest_session_id(directory: Path | None = None) -> str | None:
    """Most recently updated session id, or None when none exist."""
    items = list_sessions(directory)
    return items[0]["session_id"] if items else None
