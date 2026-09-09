from app.learning.models import LearningRecord
from app.learning.store import LearningStore


store = LearningStore()


def learn_from_result(
    *,
    mission_id: str | None,
    goal: str,
    worker: str | None,
    success: bool,
    evidence: list[dict],
    result: dict,
):
    root_cause = None
    solution = None
    tools = []

    for item in evidence:
        cmd = item.get("cmd")
        if cmd:
            tools.append(cmd.split()[0])

        if item.get("returncode", 0) != 0 and not root_cause:
            root_cause = (
                item.get("stderr")
                or item.get("stdout")
                or "execution failure"
            )[:1000]

    if success:
        solution = "Execution strategy validated by tests/evidence."

    record = LearningRecord(
        mission_id=mission_id,
        goal=goal,
        worker=worker,
        outcome="PASS" if success else "FAIL",
        root_cause=root_cause,
        solution=solution,
        tools=sorted(set(tools)),
        metadata={
            "result": result,
            "evidence_count": len(evidence),
        },
    )

    store.save(record)
    return record
