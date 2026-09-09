from app.learning.models import LearningRecord
from app.learning.store import LearningStore


store = LearningStore()

TOOL_LABELS = {
    "pytest": "pytest",
    "python": "python",
    "python3": "python",
    "npm": "npm",
    "git": "git",
}


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

    strategy = None

    if success:
        solution = "Execution strategy validated by tests/evidence."

        touched = result.get("target_files") or []
        edit_count = result.get("edit_count")

        if edit_count:
            strategy = (
                f"atomic edits[] plan ({edit_count} edit(s), "
                f"files: {', '.join(touched) if touched else 'n/a'})"
            )

    retries = result.get("retries", 0) or 0

    tags = ["repo-code" if worker == "repo-code-bud" else "code"]

    if result.get("llm_plan") is not None:
        tags.append("llm-planned")

    record = LearningRecord(
        mission_id=mission_id,
        goal=goal,
        worker=worker,
        outcome="PASS" if success else "FAIL",
        strategy=strategy,
        root_cause=root_cause,
        solution=solution,
        retries=retries,
        tools=sorted(set(tools)),
        tags=tags,
        metadata={
            "result": result,
            "evidence_count": len(evidence),
        },
    )

    store.save(record)
    return record
