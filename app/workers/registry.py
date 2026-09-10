from app.workers.code_worker import CodeWorker
from app.workers.repo_code_worker import RepoCodeWorker


class WorkerRegistry:
    def __init__(self):
        self.workers = [
            CodeWorker(),
            RepoCodeWorker(),
        ]

    def find(self, capability: str):
        for worker in self.workers:
            if worker.supports(capability):
                return worker
        return None

    def status(self):
        # Normalize every worker entry so the API shape never depends
        # on what a worker's health() happens to include.
        items = []
        for worker in self.workers:
            try:
                health = worker.health() or {}
            except Exception:
                health = {}
            caps = getattr(worker, "capabilities", None)
            if caps is None:
                caps = health.get("capabilities") or []
            items.append(
                {
                    **health,
                    "name": getattr(worker, "name", None)
                    or health.get("name")
                    or type(worker).__name__,
                    "status": health.get("status", "UNKNOWN"),
                    "capabilities": sorted(caps or []),
                }
            )
        return items


registry = WorkerRegistry()
