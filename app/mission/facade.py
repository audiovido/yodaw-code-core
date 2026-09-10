"""Worker I: thin product orchestration facade.

Calls existing Repo Intelligence, Planning, Skills, runtime, and
verification layers without duplicating their business logic. The
facade records durable lifecycle transitions and classifies the
terminal outcome so provider/network failures never collapse
into task failures.
"""

from __future__ import annotations

from typing import Any

from app.core.models import Mission, MissionStatus

PRODUCT_STATUSES = (
    "QUEUED",
    "OBSERVING",
    "PLANNING",
    "EXECUTING",
    "VERIFYING",
    "RECOVERING",
    "PASS",
    "FAIL",
    "BLOCKED_EXTERNAL",
    "CANCELLED",
)

PROVIDER_ERROR_TYPES = {
    "LLMError",
    "ProviderError",
    "TimeoutError",
    "ConnectError",
    "HTTPError",
    "ToolMissingError",
}

TASK_ERROR_TYPES = {
    "ValidationFailed",
    "InvalidEditPlan",
    "FindTextMissing",
    "TargetNotFound",
    "TargetNotFile",
    "PathEscapeError",
    "NoChange",
    "CommitError",
    "RestoreError",
    "RetryExhausted",
    "DirtyRepo",
    "RepoError",
    "ConfigError",
    "LLMBlocked",
}

MAX_RETRY_ATTEMPTS = 3


def classify_error(error: Any) -> str:
    """Classify a worker error without collapsing provider faults."""
    if not error:
        return "task"
    if isinstance(error, str):
        text = error
    elif isinstance(error, dict):
        text = str(error.get("type", "")) + " " + str(error.get("message", ""))
    else:
        text = str(error)
    lowered = text.lower()
    if any(
        token in lowered
        for token in (
            "blocked_external",
            "network",
            "timeout",
            "timed out",
            "connection",
            "provider",
            "ollama request failed",
            "unreachable",
            "dns",
        )
    ):
        return "provider"
    err_type = error.get("type", "") if isinstance(error, dict) else ""
    if err_type in PROVIDER_ERROR_TYPES:
        return "provider"
    if err_type in TASK_ERROR_TYPES:
        return "task"
    return "task"


def observe_repo(repo_path: str | None) -> dict:
    """Build repo context through the existing intelligence engine."""
    if not repo_path:
        return {"observed": False, "reason": "no repo_path provided"}
    try:
        from app.repo_intelligence import RepoIntelligence

        evidence = RepoIntelligence().build_evidence(repo_path)
        return {
            "observed": True,
            "repo_type": evidence.get("repo_type"),
            "file_count": evidence.get("file_count"),
            "tests": evidence.get("tests", [])[:25],
        }
    except Exception as exc:
        return {"observed": False, "reason": str(exc)}


def build_plan(goal: str, repo_context: dict) -> dict:
    """Decompose through the existing planning layer (no duplicate)."""
    try:
        from app.planning import AdvancedPlanner

        planner = AdvancedPlanner()
        plan = planner.create_plan(goal, context={"repo": repo_context})
        validation = planner.validate_plan(plan)
        return {
            "planned": bool(validation.valid),
            "plan_id": plan.id,
            "steps": len(plan.steps),
            "valid": validation.valid,
            "errors": list(validation.errors)[:5],
        }
    except Exception as exc:
        return {"planned": False, "reason": str(exc)}


def select_skills(goal: str, repo_context: dict) -> dict:
    """Select skills through the existing classifier/registry."""
    try:
        from app.skills import SkillRegistry, TaskClassifier
        from app.skills.selection import SelectionContext, SkillSelector
        from app.skills.skills import (
            BugfixSkill,
            DocumentationSkill,
            FeatureSkill,
            RefactorSkill,
            ReviewSkill,
            TestSkill,
        )

        registry = SkillRegistry()
        for cls in (
            BugfixSkill,
            RefactorSkill,
            TestSkill,
            ReviewSkill,
            FeatureSkill,
            DocumentationSkill,
        ):
            try:
                registry.register(cls())
            except Exception:
                continue
        classifier = TaskClassifier(
            {s.id: s for s in registry.get_all()}
        )
        classification = classifier.classify(
            goal,
            context={"language": repo_context.get("repo_type")},
        )
        selection = SkillSelector(registry).select(
            SelectionContext(
                task_description=goal,
                classification=classification,
            )
        )
        return {
            "selected": True,
            "skill": selection.selected_skill,
            "confidence": selection.confidence,
            "intent": classification.detected_intent.value,
        }
    except Exception as exc:
        return {"selected": False, "reason": str(exc)}


def transition(
    store, mission_id: str, status: MissionStatus, event: str
) -> Mission | None:
    """Persist one lifecycle transition with an event record."""
    mission = store.get(mission_id)
    if mission is None:
        return None
    mission.status = status
    store.save(mission)
    try:
        store.record_event(
            mission_id, event, attempt=mission.attempt, data={"status": status.value}
        )
    except Exception:
        pass
    return store.get(mission_id)


def run_product_lifecycle(
    store,
    mission: Mission,
    *,
    dry_run: bool = False,
    execute=None,
) -> Mission:
    """Drive observe/plan/skills stages, then hand to the runtime."""
    transition(store, mission.id, MissionStatus.observing, "mission.observing")
    repo_path = mission.metadata.get("repo_path")
    repo_context = observe_repo(repo_path)
    transition(store, mission.id, MissionStatus.planning, "mission.planning")
    plan_info = build_plan(mission.goal, repo_context)
    skill_info = select_skills(mission.goal, repo_context)
    fresh = store.get(mission.id)
    if fresh is None:
        return mission
    evidence = list(fresh.evidence)
    evidence.append(
        {
            "type": "product_context",
            "repo": repo_context,
            "plan": plan_info,
            "skills": skill_info,
        }
    )
    fresh.evidence = evidence
    if dry_run:
        fresh.status = MissionStatus.passed
        fresh.result = {
            **fresh.result,
            "dry_run": True,
            "repo_context": repo_context,
            "plan": plan_info,
            "skills": skill_info,
        }
        fresh.error_class = "task"
        store.save(fresh)
        return fresh
    fresh.status = MissionStatus.executing
    store.save(fresh)
    try:
        store.record_event(
            mission.id, "mission.executing", attempt=fresh.attempt, data={}
        )
    except Exception:
        pass
    if execute is not None:
        return execute(fresh)
    return store.get(mission.id) or fresh


def finalize_from_worker_result(store, mission_id: str, worker_result: dict) -> Mission | None:
    """Apply bounded recovery labels and error classification."""
    mission = store.get(mission_id)
    if mission is None:
        return None
    success = bool(worker_result.get("success"))
    error = worker_result.get("error")
    error_class = "task" if not success else mission.error_class
    if not success:
        error_class = classify_error(error)
    if success:
        mission.status = MissionStatus.passed
    elif error_class == "provider":
        mission.status = MissionStatus.blocked_external
    else:
        mission.status = MissionStatus.failed
    mission.error_class = error_class if not success else mission.error_class
    store.save(mission)
    return mission


def retry_allowed(mission: Mission) -> tuple[bool, str]:
    """Bounded retry policy with terminal/attempt guards."""
    if mission.status not in (
        MissionStatus.failed,
        MissionStatus.blocked_external,
        MissionStatus.blocked,
        MissionStatus.cancelled,
    ):
        return False, "mission is not in a retryable terminal state"
    attempts = mission.metadata.get("product_attempts", 1)
    if attempts >= MAX_RETRY_ATTEMPTS:
        return False, "retry budget exhausted"
    return True, ""
