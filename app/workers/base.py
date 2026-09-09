from abc import ABC, abstractmethod
from typing import Any


class WorkerResult(dict):
    pass


class Worker(ABC):
    name: str
    capabilities: set[str]

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    @abstractmethod
    def health(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def execute(self, goal: str) -> WorkerResult:
        raise NotImplementedError
