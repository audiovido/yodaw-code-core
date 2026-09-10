"""Live event model shared by the pipeline and the REPL renderer."""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable

from app.cli.redact import redact_mapping, redact_text

# Pipeline stages in canonical order.
STAGES = (
    "observe",
    "repo",
    "plan",
    "route",
    "execute",
    "test",
    "verify",
    "recover",
    "learn",
    "deliver",
    "done",
    "error",
)

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
}


def make_event(stage: str, message: str, detail: Any = None, level: str = "info") -> dict[str, Any]:
    """Build one structured pipeline event."""
    event: dict[str, Any] = {"stage": stage, "message": message, "level": level}
    if detail is not None:
        event["detail"] = detail
    return event


def supports_color(stream=None) -> bool:
    """Color only on a TTY; never in pipes or JSON mode."""
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def render_human(event: dict[str, Any], verbose: bool = False, color: bool = False) -> str:
    """Render one event as a concise `[stage] message` line."""
    stage = str(event.get("stage", "info"))
    message = redact_text(str(event.get("message", "")))
    line = f"[{stage}] {message}"
    if verbose and event.get("detail") is not None:
        detail = redact_mapping({"detail": event["detail"]})["detail"]
        try:
            rendered = json.dumps(detail, default=str)[:2000]
        except (TypeError, ValueError):
            rendered = str(detail)[:2000]
        line += f" :: {redact_text(rendered)}"
    if color and event.get("level") == "error":
        return f"{_ANSI['red']}{line}{_ANSI['reset']}"
    return line


def render_json(events: Iterable[dict[str, Any]]) -> str:
    """Render events as newline-delimited JSON without ANSI codes."""
    lines = []
    for event in events:
        safe = redact_mapping(dict(event))
        lines.append(json.dumps(safe, default=str))
    return "\n".join(lines)
