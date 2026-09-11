"""
Stage 8.11: runtime service.

Single production entrypoint:

    python -m app.runtime

Starts the API together with the embedded coordinator (which
contains the queue consumer and the watchdog). SIGTERM/SIGINT
stop claiming new missions, drain inflight missions, release
leases, and close storage without corrupting worktrees.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone

import uvicorn

from app.main import app, get_coordinator, RequestIdLogFilter

logger = logging.getLogger("yodaw.runtime")


class JsonFormatter(logging.Formatter):
    """Structured JSON log formatter with correlation IDs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Include correlation ID if available
        if hasattr(record, "request_id"):
            payload["request_id"] = record.request_id
        # Include any extra fields
        for key, value in record.__dict__.items():
            if key not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "lineno", "funcName", "created",
                "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "exc_info", "exc_text",
                "stack_info", "getMessage", "request_id"
            }:
                payload[key] = value
        return json.dumps(payload)


def _setup_logging():
    """Configure structured JSON logging."""
    level = os.environ.get("YODAW_LOG_LEVEL", "INFO").upper()
    formatter = JsonFormatter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RequestIdLogFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def main() -> int:
    _setup_logging()

    host = os.environ.get("YODAW_HOST", "127.0.0.1")
    port = int(os.environ.get("YODAW_PORT", "8844"))

    # Start the coordinator (queue consumer + watchdog) before
    # binding the socket so queued work begins as early as possible.
    coordinator = get_coordinator()

    if coordinator is None:
        logger.warning(
            "embedded coordinator disabled (YODAW_EMBED_COORDINATOR=0); "
            "run a standalone coordinator process against this database"
        )

    shutting_down = {"flag": False}

    def handle_signal(signum, frame):
        if shutting_down["flag"]:
            return

        shutting_down["flag"] = True
        logger.info("received signal %s; draining and shutting down", signum)

        if coordinator is not None:
            coordinator.stop(drain=True, timeout=25)

        # Terminate uvicorn; its own shutdown hook runs the app
        # lifespan which is a no-op for an already-stopped
        # coordinator (stop() is idempotent).
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info("YODAW runtime listening on %s:%s", host, port)

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=os.environ.get("YODAW_LOG_LEVEL", "info").lower(),
    )

    if coordinator is not None:
        coordinator.stop(drain=True, timeout=25)

    return 0


if __name__ == "__main__":
    sys.exit(main())