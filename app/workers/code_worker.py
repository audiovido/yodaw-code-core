from __future__ import annotations

import os
import time
from typing import Optional

from app.workers.repo_code_worker import CancelContext, RepoCodeWorker


class CodeWorker:
    """Deterministic local worker for the 'code' capability."""

    name = "code-bud"
    capabilities = {"code"}

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def health(self):
        return {
            "name": self.name,
            "status": "READY",
            "capabilities": sorted(self.capabilities),
        }

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

        repo_path = metadata.get("repo_path") if metadata else None

        if repo_path and os.path.isdir(repo_path):
            # Repository work delegates to the real repo worker so
            # commit_sha, tests_passed, workspace and git-clean status
            # come from actual git/pytest execution.
            return RepoCodeWorker().execute(goal, metadata)

        # No repository target: report only truthful planning/result
        # data. No repo-derived values or evidence are fabricated.
        return {
            "success": True,
            "output": {
                "goal": goal,
                "repo": {
                    "observed": False,
                },
                "plan": {
                    "planned": True,
                    "plan_id": "plan_code",
                    "steps": 1,
                    "valid": True,
                    "errors": [],
                },
                "skills": {
                    "selected": True,
                    "skill": "code",
                    "confidence": 0.9,
                    "intent": "feature",
                },
            },
            "evidence": [
                {
                    "type": "plan",
                    "message": "planning only; no repository target",
                }
            ],
            "error": None,
            "retryable": False,
        }