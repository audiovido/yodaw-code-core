from app.learning.models import LearningRecord
from app.learning.store import LearningStore


store = LearningStore()


def record_id_default(mission_id: str | None) -> str:
    """Deterministic learning-record id derived from the mission."""
    import hashlib

    digest = hashlib.sha1((mission_id or "").encode("utf-8")).hexdigest()
    return f"lr_{digest[:12]}"

TOOL_LABELS = {
    "pytest": "pytest",
    "python": "python",
    "python3": "python",
    "npm": "npm",
    "git": "git",
}


def build_learning_record(
    *,
    mission_id: str | None,
    goal: str,
    worker: str | None,
    success: bool,
    evidence: list[dict],
    result: dict,
    record_id: str | None = None,
) -> LearningRecord:
    """
    Construct a learning record without persisting it.

    Stage 9.5: the outbox relay uses this to deliver records
    through its own store instance (same database as the mission
    store), instead of depending on this module's global store.
    """
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

    # Stage 9.5: a caller-supplied deterministic id makes the
    # record upsert-idempotent, which is what lets the outbox
    # relay deliver learning exactly once even across replays.
    # Ad-hoc records without a mission keep random ids.
    record_fields = {
        "mission_id": mission_id,
        "goal": goal,
        "worker": worker,
        "outcome": "PASS" if success else "FAIL",
        "strategy": strategy,
        "root_cause": root_cause,
        "solution": solution,
        "retries": retries,
        "tools": sorted(set(tools)),
        "tags": tags,
        "metadata": {
            "result": result,
            "evidence_count": len(evidence),
        },
    }

    deterministic_id = record_id or (
        record_id_default(mission_id) if mission_id else None
    )

    if deterministic_id:
        record_fields["id"] = deterministic_id

    record = LearningRecord(**record_fields)

    return record


def learn_from_result(
    *,
    mission_id: str | None,
    goal: str,
    worker: str | None,
    success: bool,
    evidence: list[dict],
    result: dict,
    record_id: str | None = None,
) -> LearningRecord:
    """Build and persist a learning record (Stage 7 contract)."""
    record = build_learning_record(
        mission_id=mission_id,
        goal=goal,
        worker=worker,
        success=success,
        evidence=evidence,
        result=result,
        record_id=record_id,
    )

    store.save(record)
    return record
