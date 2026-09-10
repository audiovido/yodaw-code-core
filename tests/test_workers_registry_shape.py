"""The workers API shape never depends on worker health() internals."""
from app.workers.registry import WorkerRegistry


class MinimalHealthWorker:
    name = "mini-bud"
    capabilities = {"code"}

    def supports(self, capability):
        return capability in self.capabilities

    def health(self):
        return {"status": "READY"}


def test_status_normalizes_workers_missing_capability_keys():
    registry = WorkerRegistry()
    registry.workers.append(MinimalHealthWorker())
    items = registry.status()
    by_name = {item["name"]: item for item in items}
    assert by_name["mini-bud"]["capabilities"] == ["code"]
    assert by_name["mini-bud"]["status"] == "READY"
    assert by_name["code-bud"]["capabilities"] == ["code"]
    caps = {c for item in items for c in item["capabilities"]}
    assert "code" in caps
