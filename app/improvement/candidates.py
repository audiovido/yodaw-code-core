"""Skill/policy candidate generation from failure clusters.

Deterministic, evidence-backed candidate builder: each cluster maps to
one skill candidate and/or one policy candidate whose content is
derived from the cluster's failure modes. Unsafe content (actuation,
policy bypass, network exfiltration) is rejected here, before a
proposal record is ever created.

Integrates the existing Skills core by grounding candidates in real
skill ids from :class:`app.skills.registry.SkillRegistry`:
``target_skill_id`` names the skill the candidate improves, and
``base_skill_version`` records the version it was generated against.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.learning.clusters import FailureCluster

# Substrings that mark candidate content as unsafe. A candidate whose
# serialized content contains any of these is rejected outright.
UNSAFE_CONTENT_SUBSTRINGS = (
    "auto_approve",
    "bypass",
    "disable_validation",
    "ignore_regression",
    "override_score",
    "exfiltrate",
    "reverse_shell",
    "curl ",
    "wget ",
    "socket.",
    "subprocess",
    "os.system",
)

# Risk weight per failure mode; unknown modes get the default weight.
RISK_WEIGHTS = {
    "test_regression": 0.30,
    "wrong_file": 0.25,
    "over_edit": 0.20,
    "bad_edit": 0.20,
    "syntax_error": 0.10,
    "validation_error": 0.15,
    "evidence_missing": 0.10,
    "repair_loop_exhausted": 0.15,
    "commit_gate_failure": 0.20,
    "false_positive_success": 0.35,
}
DEFAULT_RISK_WEIGHT = 0.10

# Any candidate scoring above this is refused before proposal creation.
MAX_ACCEPTABLE_RISK = 0.85

# Failure modes that call for a policy guardrail rather than skill text.
POLICY_MODES = {
    "wrong_file",
    "over_edit",
    "commit_gate_failure",
    "false_positive_success",
}


def content_hash_for(content: dict) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def risk_for_modes(modes: list[str]) -> float:
    """Deterministic risk score in [0, 1] for a set of failure modes."""
    if not modes:
        return DEFAULT_RISK_WEIGHT
    total = sum(RISK_WEIGHTS.get(m, DEFAULT_RISK_WEIGHT) for m in modes)
    # Diminishing combination keeps the score bounded in [0, 1).
    combined = 1.0 - 1.0 / (1.0 + total)
    return round(min(0.99, max(0.01, combined)), 4)


def find_unsafe_markers(content: dict) -> list[str]:
    """Return unsafe substrings present in serialized content."""
    blob = json.dumps(content, sort_keys=True).lower()
    return [token for token in UNSAFE_CONTENT_SUBSTRINGS if token in blob]


@dataclass
class SkillCandidate:
    kind: str
    title: str
    description: str
    content: dict
    content_hash: str
    expected_impact: str
    risk_score: float
    validation_plan: list[str]
    target_skill_id: str | None = None
    base_skill_version: str | None = None


def _target_skill_for(
    cluster: FailureCluster, registry=None
) -> tuple[str | None, str | None]:
    """Ground the candidate in a real skill from the Skills core.

    Returns (skill_id, version). Falls back to (None, None) when no
    registry is supplied or no skill matches.
    """
    if registry is None:
        return None, None
    try:
        from app.skills.models import Intent as SkillIntent

        intent_map = {
            "FAIL_CORRECTNESS": SkillIntent.BUGFIX,
            "FAIL_REGRESSION": SkillIntent.REVIEW,
            "FAIL_SCOPE": SkillIntent.REVIEW,
            "FAIL_EVIDENCE": SkillIntent.TEST,
            "FAIL_TIMEOUT": SkillIntent.PERFORMANCE,
            "FAIL_TOOLING": SkillIntent.DEPENDENCY,
        }
        intent = intent_map.get(cluster.result_class)
        skills = (
            registry.get_by_intent(intent, enabled_only=False)
            if intent is not None
            else registry.get_all()
        )
        if not skills:
            skills = registry.get_all()
        if not skills:
            return None, None
        skill = sorted(skills, key=lambda s: s.id)[0]
        return skill.id, skill.version
    except Exception:
        return None, None


def _validation_plan_for(cluster: FailureCluster) -> list[str]:
    plan = [
        f"replay {len(cluster.case_ids)} clustered cases from {cluster.id}",
        "compare benchmark before/after on the clustered suite",
        "run regression suite; reject on any new failure",
    ]
    if "test_regression" in cluster.failure_modes:
        plan.append("run unrelated-tests subset to confirm no regressions")
    if "syntax_error" in cluster.failure_modes:
        plan.append("syntax-check generated guidance examples")
    return plan


def generate_candidates(
    cluster: FailureCluster, registry=None
) -> tuple[list[SkillCandidate], list[str]]:
    """Build skill/policy candidates for one cluster.

    Returns (candidates, rejection_reasons). Unsafe candidates are
    refused here; the reasons explain why nothing was produced.
    """
    risk = risk_for_modes(cluster.failure_modes)
    if risk > MAX_ACCEPTABLE_RISK:
        return [], [f"risk {risk} exceeds maximum {MAX_ACCEPTABLE_RISK}"]

    target_id, target_version = _target_skill_for(cluster, registry)
    modes = ", ".join(cluster.failure_modes) or "unknown"
    guidance = (
        f"When handling {cluster.result_class} with {modes}, "
        "apply the checked procedure: reproduce first, change the "
        "smallest scope that addresses the root cause, then re-run "
        "the clustered cases before committing."
    )

    candidates: list[SkillCandidate] = []
    skill_content = {
        "cluster_id": cluster.id,
        "signature": cluster.signature,
        "guidance": guidance,
        "failure_modes": list(cluster.failure_modes),
        "case_ids": list(cluster.case_ids),
        "target_skill_id": target_id,
        "base_skill_version": target_version,
    }
    unsafe = find_unsafe_markers(skill_content)
    if unsafe:
        return [], [f"unsafe content markers: {sorted(unsafe)}"]

    candidates.append(
        SkillCandidate(
            kind="SKILL",
            title=f"Guidance for {cluster.result_class} ({modes})",
            description=(
                f"Skill guidance distilled from {cluster.size} recurring "
                f"failures in cluster {cluster.id}."
            ),
            content=skill_content,
            content_hash=content_hash_for(skill_content),
            expected_impact=(
                f"Reduce {cluster.result_class} recurrences for "
                f"{cluster.size} clustered cases (avg score {cluster.avg_score:.1f})."
            ),
            risk_score=risk,
            validation_plan=_validation_plan_for(cluster),
            target_skill_id=target_id,
            base_skill_version=target_version,
        )
    )

    if any(m in POLICY_MODES for m in cluster.failure_modes):
        policy_content = {
            "cluster_id": cluster.id,
            "rule": (
                "require explicit approval before edits outside the "
                "task scope identified for " + cluster.signature
            ),
            "failure_modes": list(cluster.failure_modes),
        }
        unsafe_policy = find_unsafe_markers(policy_content)
        if not unsafe_policy:
            candidates.append(
                SkillCandidate(
                    kind="POLICY",
                    title=f"Scope guardrail for {cluster.signature}",
                    description=(
                        "Policy guardrail requiring approval for "
                        "out-of-scope edits seen in this cluster."
                    ),
                    content=policy_content,
                    content_hash=content_hash_for(policy_content),
                    expected_impact=(
                        "Block scope violations before they become regressions."
                    ),
                    risk_score=min(0.99, round(risk + 0.05, 4)),
                    validation_plan=_validation_plan_for(cluster),
                    target_skill_id=target_id,
                    base_skill_version=target_version,
                )
            )

    return candidates, []
