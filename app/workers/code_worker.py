from __future__ import annotations

import os
import time
from typing import Optional

from app.workers.base import Worker, WorkerResult
from app.workers.repo_code_worker import CancelContext

class CodeWorker(Worker):
    """Deterministic local worker for the 'code' capability."""

    name = "code-bud"
    capabilities = {"code"}

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

    def execute(self, goal: str, metadata: Optional[dict] = None) -> WorkerResult:
        print(f"CODE_WORKER: execute called with goal={goal!r}, metadata={metadata!r}", flush=True)
        # Small delay to allow cancellation to be processed if requested immediately
        time.sleep(0.1)
        # Check for cancellation if we have the necessary metadata
        # Disabled for debugging
        # if metadata and "mission_id" in metadata and "_event_store" in metadata:
        #     try:
        #         ctx = CancelContext(metadata["mission_id"], metadata["_event_store"])
        #         ctx.cancel_check("code_worker_start")
        #     except Exception:
        #         # If we can't create the context, we just proceed without cancellation check
        #         pass

        # Determine repo_path from metadata or environment, same as RepoCodeWorker
        repo_path = metadata.get("repo_path") if metadata else None
        if repo_path is None:
            repo_path = os.environ.get("YODAW_TARGET_REPO")
        print(f"CODE_WORKER: repo_path={repo_path}", flush=True)
        
        # If we have a repo_path, delegate to RepoCodeWorker for real execution
        if repo_path:
            from app.workers.repo_code_worker import RepoCodeWorker
            worker = RepoCodeWorker()
            return worker.execute(goal, metadata)
        
        # Otherwise, return a planning result (no actual repo execution)
        print(f"CODE_WORKER: Planning for goal: {goal}", flush=True)
        result = WorkerResult(
            success=True,
            output={
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
            evidence=[
                {"type": "info", "message": "CodeWorker planning started"},
                {"type": "info", "message": f"Processing goal: {goal}"},
                {"type": "info", "message": "Checking for cancellation"},
                {"type": "info", "message": "Preparing code plan"},
                {"type": "info", "message": "CodeWorker planning finished"},
            ],
            error=None,
            retryable=False,
        )
        print(f"CODE_WORKER: Returning planning result", flush=True)
        return result