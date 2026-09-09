from datetime import datetime, timezone

from app.core.models import Mission, MissionStatus
from app.storage.sqlite_store import MissionStore
from app.workers.registry import registry
from app.learning.engine import learn_from_result


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

        learn_from_result(
            mission_id=mission.id,
            goal=mission.goal,
            worker=None,
            success=False,
            evidence=[],
            result=mission.result,
        )

        return mission

    mission.worker = worker.name

    try:
        if mission.capability == "repo-code":
            result = worker.execute(
                mission.goal,
                mission.metadata,
            )
        else:
            result = worker.execute(mission.goal)

        mission.evidence.extend(result.get("evidence", []))

        mission.result = {
            **result.get("output", {}),
        }

        if result.get("error"):
            mission.result["error"] = result["error"]

        mission.status = (
            MissionStatus.passed
            if result.get("success")
            else MissionStatus.failed
        )

        learn_from_result(
            mission_id=mission.id,
            goal=mission.goal,
            worker=mission.worker,
            success=result.get("success", False),
            evidence=mission.evidence,
            result=mission.result,
        )

    except Exception as exc:
        mission.status = MissionStatus.failed
        mission.result = {
            "error": str(exc),
            "type": type(exc).__name__,
        }

        learn_from_result(
            mission_id=mission.id,
            goal=mission.goal,
            worker=mission.worker,
            success=False,
            evidence=mission.evidence,
            result=mission.result,
        )

    mission.updated_at = now_iso()
    store.save(mission)

    return mission
