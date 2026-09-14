"""
Quality / Complexity Gate.

Penalizes unnecessary rewrites, scope creep, duplicated logic, dead
code, placeholder implementations, TODO-as-solution, broad exception
swallowing, hard-coded hacks, test deletion, and validation weakening.

Deterministic checks over the diff + plan; integrates with the
review engine's defect scanner and emits a quality score.

Phase 9 of the Elite Coding Intelligence layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

from app.intelligence.review_engine import defects_in_diff


@dataclass
class QualityReport:
    """Quality assessment of a change."""

    score: float                  # 0..1
    verdict: str                  # "clean" | "acceptable" | "overbuilt" | "reject"
    issues: List[dict] = field(default_factory=list)
    metrics: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 3),
            "verdict": self.verdict,
            "issues": self.issues,
            "metrics": self.metrics,
        }


def _diff_stats(diff_text: str) -> Dict[str, int]:
    added = removed = 0
    changed_files = 0
    current = None
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            if line.startswith("+++"):
                changed_files += 1
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return {
        "added_lines": added,
        "removed_lines": removed,
        "changed_files": changed_files,
    }


def _file_touch_counts(diff_text: str) -> int:
    files = set()
    for line in diff_text.splitlines():
        m = re.match(r"\+\+\+\s+(?:b/)?(.*)", line)
        if m:
            files.add(m.group(1))
    return len(files)


def assess_quality(
    diff_text: str,
    *,
    task_type: str = "unknown",
    files_touched: int = 0,
    tests_deleted: int = 0,
) -> QualityReport:
    """Score a change for minimality and discipline."""
    issues: List[dict] = []
    stats = _diff_stats(diff_text)
    score = 1.0

    def issue(category: str, severity: str, message: str, **meta):
        issues.append(
            {"category": category, "severity": severity, "message": message, **meta}
        )

    added = stats["added_lines"]
    removed = stats["removed_lines"]
    n_files = files_touched or _file_touch_counts(diff_text)

    # Unnecessary rewrite: a file replaced almost entirely.
    if added > 200 and removed > 200:
        issue(
            "rewrite",
            "blocker",
            f"large rewrite ({added}+/{removed}-); prefer smallest correct patch",
            added=added,
            removed=removed,
        )
        score -= 0.45

    # Scope creep: many files for a small task.
    if n_files >= 6 and task_type not in ("migration", "architec", "refactor"):
        issue(
            "scope_creep",
            "warning",
            f"{n_files} files touched for a {task_type} task",
        )
        score -= 0.2
    elif n_files >= 12:
        issue("scope_creep", "blocker", f"{n_files} files touched; scope is out of control")
        score -= 0.4

    # Test deletion is always a blocker.
    if tests_deleted > 0:
        issue("test_deletion", "blocker", f"{tests_deleted} test(s) deleted")
        score -= 0.7

    # Reuse the review engine's defect scanner for anti-patterns.
    for d in defects_in_diff(diff_text):
        issue(d["category"], d["severity"], d["message"], evidence=d.get("evidence", ""))
        if d["severity"] == "blocker":
            score -= 0.3
        else:
            score -= 0.1

    # Penalize very large additions that exceed the task's natural size.
    if added > 300 and task_type in ("bug_fix", "concurrency_bug"):
        issue(
            "overbuild",
            "warning",
            f"{added} added lines for a fix task; suspicious overbuild",
        )
        score -= 0.15

    # Dead code / no-op additions: changed files with zero diff.
    if added == 0 and removed == 0 and n_files > 0:
        issue("no_change", "blocker", "diff contains no actual changes")
        score -= 0.4

    score = max(0.0, min(1.0, score))
    if tests_deleted > 0:
        verdict = "reject"
    elif score >= 0.85:
        verdict = "clean"
    elif score >= 0.6:
        verdict = "acceptable"
    elif score >= 0.35:
        verdict = "overbuilt"
    else:
        verdict = "reject"

    return QualityReport(
        score=score,
        verdict=verdict,
        issues=issues,
        metrics={"added_lines": float(added), "removed_lines": float(removed), "files_changed": float(n_files)},
    )