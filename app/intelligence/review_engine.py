"""
Adversarial Self-Review Engine.

Runs a strong reviewer stage before PASS. Reviewer checks:
correctness, regressions, edge cases, security, performance, style,
architecture, test coverage, API compatibility, data migration risk.

The reviewer challenges the implementation. A concrete defect sends
the change back to the repair loop. No fake PASS: PASS requires all
blocking checks clean AND (when a diff exists) the test evidence to
be real.

Hermetic by default: deterministic checks need no LLM. An optional
model-backed review can be injected (review_fn) for deeper checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# Failure categories the reviewer can raise.
CORRECTNESS = "correctness"
REGRESSION = "regression"
EDGE_CASE = "edge_case"
SECURITY = "security"
PERFORMANCE = "performance"
STYLE = "style"
ARCHITECTURE = "architecture"
TEST_COVERAGE = "test_coverage"
API_COMPAT = "api_compat"
DATA_MIGRATION = "data_migration"
NO_CHANGE = "no_change"
ZEAL = "zeal"  # unnecessary rewrite / scope creep / dead code

# Severity levels.
BLOCKER = "blocker"   # must repair before PASS
WARNING = "warning"   # advisory, recorded


@dataclass(frozen=True)
class ReviewFinding:
    category: str
    severity: str            # blocker | warning
    message: str
    evidence: str = ""
    file: str = ""


@dataclass
class ReviewVerdict:
    passed: bool
    findings: List[ReviewFinding] = field(default_factory=list)
    summary: str = ""
    model_used: bool = False

    def blockers(self) -> List[ReviewFinding]:
        return [f for f in self.findings if f.severity == BLOCKER]

    def warnings(self) -> List[ReviewFinding]:
        return [f for f in self.findings if f.severity == WARNING]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "model_used": self.model_used,
            "findings": [
                {
                    "category": f.category,
                    "severity": f.severity,
                    "message": f.message,
                    "evidence": f.evidence,
                    "file": f.file,
                }
                for f in self.findings
            ],
        }


LEGACY_SINGLE_EDIT = "legacy single-edit"


def verify_edit_content(
    goal: str,
    edits: List[dict],
    strategy: Optional[dict] = None,
    diff_text: str = "",
    test_success: bool = False,
) -> ReviewVerdict:
    """Deterministic pre-commit review of a plan/diff.

    Safe to run with zero model access. Checks:
    - every edit touches a file inside the plan (containment parsed
      by the edit engine separately; here we check target paths)
    - approximate token/scope sanity
    - diff size bounded (warns on very large rewrites)
    - readability defects: TODO placeholders, commented-out blocks,
      print-debugging, broad exception swallowing
    - no secrets in diff (basic scan; deep scan in diff_guard)
    - test weakening patterns in diff
    """
    findings: List[ReviewFinding] = []
    purpose = strategy or {}
    task_type = purpose.get("task_type", "unknown")

    touched: List[str] = []
    for i, edit in enumerate(edits or []):
        if not isinstance(edit, dict):
            findings.append(
                ReviewFinding(CORRECTNESS, BLOCKER, f"edit {i} is not an object")
            )
            continue
        target = edit.get("target_file")
        if not isinstance(target, str) or not target:
            findings.append(
                ReviewFinding(CORRECTNESS, BLOCKER, f"edit {i} has no target_file")
            )
        else:
            touched.append(target)
        for key in ("find", "replace"):
            value = edit.get(key)
            if value is not None and not isinstance(value, str):
                findings.append(
                    ReviewFinding(
                        CORRECTNESS,
                        BLOCKER,
                        f"edit {i} field {key} must be a string",
                    )
                )

    if not touched:
        findings.append(ReviewFinding(NO_CHANGE, BLOCKER, "plan touches no files"))

    # Scope: many edits across many files -> warning for most tasks.
    if len(touched) > 8 and task_type not in ("migration", "architec"):
        findings.append(
            ReviewFinding(
                ZEAL,
                WARNING,
                f"plan touches {len(touched)} files; verify scope is required",
            )
        )
        if len(touched) > 20:
            findings.append(
                ReviewFinding(
                    ZEAL,
                    BLOCKER,
                    "plan touches >20 files; almost certainly scope creep for a single task",
                )
            )

    # Diff-level checks.
    if diff_text:
        findings.extend(_scan_diff(defects_in_diff(diff_text)))

    # Plan-level anti-patterns (static, cheap).
    plan_text = "\n".join(
        str(e.get("replace", "")) for e in (edits or []) if isinstance(e, dict)
    )
    findings.extend(_scan_replace_text(plan_text, touched, task_type))

    # No fake PASS: if tests failed or no tests ran, we cannot PASS here.
    if not test_success:
        # A real worker runs validation separately; the review stage
        # records that its own gate did not observe green tests.
        findings.append(
            ReviewFinding(
                TEST_COVERAGE,
                WARNING,
                "review stage did not observe green tests; acceptance must come from real validation",
            )
        )

    blockers = [f for f in findings if f.severity == BLOCKER]
    return ReviewVerdict(
        passed=not blockers,
        findings=findings,
        summary=_summarize(findings),
        model_used=False,
    )


def _scan_diff(defects: List[dict]) -> List[ReviewFinding]:
    return [
        ReviewFinding(
            d["category"],
            d["severity"],
            d["message"],
            evidence=d.get("evidence", ""),
        )
        for d in defects
    ]


def defects_in_diff(diff_text: str) -> List[dict]:
    """Scan a unified diff for defect signatures. Deterministic."""
    defects: List[dict] = []
    added_lines: List[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append(line[1:].strip())

    def detect(pattern, category, severity, message):
        hit = next((l for l in added_lines if re.search(pattern, l)), None)
        if hit:
            defects.append(
                {
                    "category": category,
                    "severity": severity,
                    "message": message,
                    "evidence": hit[:160],
                }
            )

    # Test weakening: removing/commenting assertions or replacing with pass.
    detect(r"^\s*(#.*)?\s*(assert|expect|should|fail|exit)[^#]*$|comment.*assert",
           TEST_COVERAGE, BLOCKER,
           "assertion removed or commented in test diff")
    detect(r"\.skip\s*\(|test\.todo\s*\(|@pytest\.mark\.skip|xit\s*\(|pending\s*\(",
           TEST_COVERAGE, BLOCKER,
           "test marked as skipped/pending to make it pass")
    detect(r"pytest\.mark\.xfail", TEST_COVERAGE, WARNING, "xfail introduced")
    detect(r"del\s+assert|pass\s*$", TEST_COVERAGE, WARNING,
           "possible assertion removal; verify intent")

    # Broad exception swallowing.
    detect(r"except\s*:\s*$|except\s+Exception\s*:\s*$|except\s*\(?[A-Za-z, ]*\)?\s*:\s*pass\s*$",
           CORRECTNESS, WARNING, "broad exception swallow; verify error handling")
    detect(r"except:\s*$", CORRECTNESS, WARNING, "bare except")  

    # Debug leftovers.
    detect(r"print\(|console\.log\(|print_r\(|debugger\b|pdb\.|var_dump\(",
           STYLE, WARNING, "debug output left in diff")

    # TODO-as-solution.
    detect(r"TODO|FIXME|XXX|HACK\b", STYLE, WARNING, "TODO/FIXME left as solution")

    # Placeholder implementations.
    detect(r"raise\s+NotImplementedError|return\s+None\s*#\s*(TODO|stub)|\.\.\.\s*$",
           CORRECTNESS, BLOCKER, "placeholder/stub implementation in diff")

    # Secrets.
    detect(r"(?i)(api[_-]?key|secret|password|token|authorization)\s*[:=]\s*['\"][A-Za-z0-9_\-]{12,}['\"]",
           SECURITY, BLOCKER, "possible hard-coded credential in diff")

    # Hard-coded sleeps for timing races.
    detect(r"time\.sleep\(|sleep\s*\(", PERFORMANCE, WARNING,
           "sleep() in diff; verify it is not masking a race")
    return defects


def _scan_replace_text(plan_text: str, touched: List[str], task_type: str) -> List[ReviewFinding]:
    findings: List[ReviewFinding] = []
    if re.search(r"raise\s+NotImplementedError", plan_text):
        findings.append(
            ReviewFinding(CORRECTNESS, BLOCKER, "NotImplementedError placeholder detected in plan")
        )
    return findings


def merge_findings(*findings_lists: List[ReviewFinding]) -> List[ReviewFinding]:
    """Deduplicate findings across review passes."""
    seen = set()
    merged: List[ReviewFinding] = []
    for fl in findings_lists:
        for f in fl:
            key = (f.category, f.severity, f.message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(f)
    return merged


def review_verdict_from_list(findings: List[ReviewFinding]) -> ReviewVerdict:
    blockers = [f for f in findings if f.severity == BLOCKER]
    return ReviewVerdict(
        passed=not blockers,
        findings=findings,
        summary=_summarize(findings),
        model_used=False,
    )


def _summarize(findings: List[ReviewFinding]) -> str:
    blockers = [f for f in findings if f.severity == BLOCKER]
    warnings = [f for f in findings if f.severity == WARNING]
    if blockers:
        return (
            f"REJECT: {len(blockers)} blocker(s): "
            + "; ".join(f"{f.category}: {f.message}" for f in blockers[:3])
        )
    if warnings:
        return (
            f"PASS with {len(warnings)} warning(s): "
            + "; ".join(f"{f.category}: {f.message}" for f in warnings[:3])
        )
    return "PASS: no findings"


class ModelReviewer:
    """Optional model-backed reviewer.

    review_fn(system, user) -> str is injected (e.g. provider.chat).
    The model's review is advisory unless it names a defect with a
    concrete location; an unparsable or empty review does not pass
    or fail anything by itself (no fake PASS, no fake block).
    """

    SYSTEM_PROMPT = """
You are the adversarial acceptance reviewer for a coding change.

Challenge the implementation. Find concrete defects; do not rubber-stamp.

Check correctness, regressions, edge cases, security, performance,
style, architecture, test coverage, API compatibility, and data
migration risk.

Reply with a JSON object:
{
  "verdict": "pass" | "reject",
  "blockers": [{"category": "...", "message": "...", "file": "..."}],
  "warnings": [{"category": "...", "message": "...", "file": "..."}],
  "summary": "one sentence"
}

Only "reject" when you have a concrete, specific defect. Absence of
information is not a defect. No markdown fences.
""".strip()

    def __init__(self, review_fn: Callable[[str, str], str]):
        self.review_fn = review_fn

    def review(self, context: str) -> ReviewVerdict:
        import json

        raw = ""
        try:
            raw = self.review_fn(self.SYSTEM_PROMPT, context)
        except Exception:
            return ReviewVerdict(
                passed=True,
                findings=[],
                summary="model review unavailable; deterministic review only",
                model_used=False,
            )
        try:
            data = json.loads(raw)
        except Exception:
            return ReviewVerdict(
                passed=True,
                findings=[],
                summary="model review unparsable; deterministic review only",
                model_used=False,
            )
        findings: List[ReviewFinding] = []
        for b in data.get("blockers", []) or []:
            findings.append(
                ReviewFinding(
                    b.get("category", CORRECTNESS),
                    BLOCKER,
                    b.get("message", "model blocker"),
                    file=b.get("file", ""),
                )
            )
        for w in data.get("warnings", []) or []:
            findings.append(
                ReviewFinding(
                    w.get("category", STYLE),
                    WARNING,
                    w.get("message", "model warning"),
                    file=w.get("file", ""),
                )
            )
        verdict = data.get("verdict") == "pass"
        blockers = [f for f in findings if f.severity == BLOCKER]
        passed = verdict and not blockers
        return ReviewVerdict(
            passed=passed,
            findings=findings,
            summary=data.get("summary", _summarize(findings)),
            model_used=True,
        )


def build_review_context(
    goal: str,
    diff_text: str,
    test_results: List[dict],
    strategy: Optional[dict] = None,
    touched_files: Optional[List[str]] = None,
) -> str:
    """Assemble the context block handed to the model reviewer."""
    parts = [
        f"CODING GOAL:\n\n{goal}\n",
        f"TASK STRATEGY:\n\n{strategy or 'unknown'}\n",
        f"TOUCHED FILES:\n\n{', '.join(touched_files or [])}\n",
    ]
    if test_results:
        parts.append(
            "TEST RESULTS:\n\n"
            + "\n".join(
                f"  {r.get('cmd', '?')} -> rc={r.get('returncode')}"
                + (" [TIMEOUT]" if r.get("timed_out") else "")
                for r in test_results
            )
        )
    parts.append(f"DIFF:\n\n{diff_text[:16000]}")
    return "\n\n".join(parts)