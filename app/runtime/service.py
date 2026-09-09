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

import logging
import os
import signal
import sys

import uvicorn

from app.main import app, get_coordinator


logger = logging.getLogger("yodaw.runtime")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("YODAW_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
