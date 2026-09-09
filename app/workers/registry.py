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
        return [worker.health() for worker in self.workers]


registry = WorkerRegistry()
