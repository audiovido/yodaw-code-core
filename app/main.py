from fastapi import FastAPI, HTTPException

from app.core.models import Mission, MissionCreate
from app.core.orchestrator import run_mission, store
from app.workers.registry import registry
from app.learning.engine import store as learning_store


app = FastAPI(
    title="YODAW Code Core",
    version="0.1.0",
)


@app.get("/api/v1/health")
def health():
    return {
        "service": "YODAW",
        "status": "READY",
        "workers": registry.status(),
    }


@app.get("/api/v1/workers")
def workers():
    return registry.status()


@app.post("/api/v1/missions")
def create_mission(request: MissionCreate):
    mission = Mission(
        goal=request.goal,
        capability=request.capability,
        metadata=request.metadata,
    )

    store.save(mission)

    return run_mission(mission)


@app.get("/api/v1/missions")
def list_missions():
    return store.list()


@app.get("/api/v1/missions/{mission_id}")
def get_mission(mission_id: str):
    mission = store.get(mission_id)

    if not mission:
        raise HTTPException(404, "Mission not found")

    return mission


@app.get("/api/v1/missions/{mission_id}/evidence")
def get_evidence(mission_id: str):
    mission = store.get(mission_id)

    if not mission:
        raise HTTPException(404, "Mission not found")

    return {
        "mission_id": mission.id,
        "evidence": mission.evidence,
    }


@app.get("/api/v1/learning")
def list_learning():
    return learning_store.list()
