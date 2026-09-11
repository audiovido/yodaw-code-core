from __future__ import annotations

import time
from typing import Optional

from app.workers.repo_code_worker import CancelContext


class CodeWorker:
    """Deterministic local worker for the 'code' capability."""

    capabilities = {"code"}

    def execute(self, goal: str, metadata: Optional[dict] = None) -> dict:
        # Small delay to allow cancellation to be processed if requested immediately
        time.sleep(0.1)
        # Check for cancellation if we have the necessary metadata
        if metadata and "mission_id" in metadata and "_event_store" in metadata:
            try:
                ctx = CancelContext(metadata["mission_id"], metadata["_event_store"])
                ctx.cancel_check("code_worker_start")
            except Exception:
                # If we can't create the context, we just proceed without cancellation check
                pass

        """Always succeed."""
        return {
            "success": True,
            "output": {
                "goal": goal,
                "repo": {
                    "observed": False
                },
                "plan": {
                    "planned": True,
                    "plan_id": "plan_code",
                    "steps": 1,
                    "valid": True,
                    "errors": []
                },
                "skills": {
                    "selected": True,
                    "skill": "code",
                    "confidence": 0.9,
                    "intent": "feature"
                }
            },
            "evidence": [],
            "error": None,
            "retryable": False,
        }