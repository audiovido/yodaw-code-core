"""AUDIT DIAGNOSTIC (not production code).

Proves that the adversarial self-review gate in
``app/workers/repo_code_worker.py`` is dead code for every finding produced
by ``app.intelligence.review_engine.verify_edit_content``.

The worker builds ``review_findings`` from ``reviewed.findings`` (a list of
``ReviewFinding`` dataclasses) and then filters:

    blockers = [f for f in review_findings
                if isinstance(f, dict) and f.get("severity") == "blocker"]

``isinstance(ReviewFinding(...), dict)`` is False, so ``blockers`` is always
empty and the fail-closed branch is never taken - even though the same
finding is serialised into the mission evidence via
``reviewed.to_dict()["findings"]``, where it reads as a blocker.

Run against the release worktree:

    PYTHONPATH=/home/user/yodaw-v1-audit \
      python -m pytest audit/test_self_review_dead_code.py -q
"""
from __future__ import annotations

import inspect

from app.intelligence.review_engine import ReviewFinding, verify_edit_content


def _worker_blocker_filter(review_findings):
    """Verbatim copy of the filter at app/workers/repo_code_worker.py:1745."""
    return [
        f
        for f in review_findings
        if isinstance(f, dict) and f.get("severity") == "blocker"
    ]


def test_review_findings_are_not_dicts():
    finding = ReviewFinding(category="test_coverage", severity="blocker",
                            message="m", evidence="e")
    assert finding.severity == "blocker"
    assert not isinstance(finding, dict), "ReviewFinding is a dataclass, not a dict"


def test_the_worker_filter_drops_every_review_engine_blocker():
    """The exact D-02 attack: weaken the acceptance test."""
    reviewed = verify_edit_content(
        "Make add() add its arguments",
        [{"target_file": "test_calc.py",
          "find": "    assert add(2, 3) == 5",
          "replace": "    assert True"}],
        diff_text="-    assert add(2, 3) == 5\n+    assert True\n",
        test_success=True,
    )

    # the review engine itself is correct
    assert reviewed.passed is False
    assert any(f.severity == "blocker" for f in reviewed.findings)

    # ...but the worker's blocker filter sees nothing
    blockers = _worker_blocker_filter(list(reviewed.findings))
    assert blockers == [], (
        "expected the buggy filter to drop the blocker; if this now fails, "
        "the defect has been fixed")

    # ...and the evidence still reports it, which is what makes it invisible
    serialised = reviewed.to_dict()["findings"]
    assert serialised and serialised[0]["severity"] == "blocker"


def test_the_bug_is_the_isinstance_dict_check_in_the_shipped_source():
    """Pin the root cause to the shipped line, not to a paraphrase."""
    import app.workers.repo_code_worker as w

    src = inspect.getsource(w)
    assert "if isinstance(f, dict) and f.get(\"severity\") == \"blocker\"" in src, (
        "the shipped blocker filter has changed; re-audit")
    assert "review_findings.extend(reviewed.findings)" in src, (
        "the shipped code no longer extends with raw ReviewFinding objects")


def test_placeholder_implementation_blocker_is_also_dropped():
    """verify_edit_content rejects NotImplementedError stubs too."""
    reviewed = verify_edit_content(
        "implement feature",
        [{"target_file": "app.py", "find": "x", "replace": "raise NotImplementedError"}],
        test_success=True,
    )
    assert reviewed.passed is False
    assert _worker_blocker_filter(list(reviewed.findings)) == []


def test_scope_creep_blocker_is_also_dropped():
    reviewed = verify_edit_content(
        "small refactor",
        [{"target_file": f"file_{i}.py", "find": f"v{i}", "replace": "z"}
         for i in range(25)],
        test_success=True,
    )
    assert reviewed.passed is False
    assert _worker_blocker_filter(list(reviewed.findings)) == []
