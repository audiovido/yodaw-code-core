from datetime import datetime, timezone

from app.core.models import Mission, MissionStatus
from app.storage.sqlite_store import MissionStore
from app.workers.registry import registry


store = MissionStore()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def run_mission(mission: Mission) -> Mission:
    mission.status = MissionStatus.running
    mission.updated_at = now_iso()
    store.save(mission)

    worker = registry.find(mission.capability)

    if not worker:
        mission.status = MissionStatus.blocked
        mission.result = {
            "error": f"No worker for capability: {mission.capability}"
        }
        mission.updated_at = now_iso()
        store.save(mission)
        return mission

    mission.worker = worker.name

    try:
        result = worker.execute(mission.goal)

        mission.evidence.extend(result.get("evidence", []))
        mission.result = result.get("output", {})

        mission.status = (
            MissionStatus.passed
            if result.get("success")
            else MissionStatus.failed
        )

    except Exception as exc:
        mission.status = MissionStatus.failed
        mission.result = {
            "error": str(exc),
            "type": type(exc).__name__,
        }

    mission.updated_at = now_iso()
    store.save(mission)

    return mission
