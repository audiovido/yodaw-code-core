"""Kodgar Verifier: the only authority on whether a task passed.

The executor is a suspect, not a witness. A coding agent — or a model —
saying "done" is worth nothing here. Every claim must be backed by real
state on disk and by real process exit codes:

- a worktree really exists on the planned branch
- the filesystem really changed (git says so)
- every file the plan promised really exists
- planned exact content really matches, byte for byte
- changed files really stay inside the declared scope
- tests really ran, in *this* worktree, and really exited 0
- a build really ran when the plan promised one
- a commit really exists, is a real commit, and contains those changes

A check that cannot be executed is reported ``SKIPPED`` with the
reason — never quietly upgraded to a pass. ``PASS`` requires the
mandatory evidence set plus zero failing checks.

The pre-commit report and the post-commit report are produced by the
same object so the API returns one coherent ``VerificationReport``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from app.background.models import (
    PlanResult,
    TaskRecord,
    VerificationCheck,
    VerificationReport,
)
from app.workers.safe_subprocess import run
from app.workers.worker_errors import ToolMissingError

# Checks that must be PASS for a task to be allowed to commit.
MANDATORY_CHECKS = ("worktree", "files_changed", "expected_files", "syntax")

PYTEST_COUNT = re.compile(
    r"(?P<passed>\d+) passed(?:, (?P<failed>\d+) failed)?"
)
PYTEST_FAILED = re.compile(r"(?P<failed>\d+) failed")
JEST_COUNT = re.compile(r"Tests?:\s+(?P<failed>\d+) failed,\s+(?P<passed>\d+) passed")
JEST_ALL = re.compile(r"Tests?:\s+(?P<passed>\d+) passed")


class TaskVerifier:
    """Runs the real evidence checks for one task."""

    def __init__(self, cancel_check=None, on_event=None):
        self.cancel_check = cancel_check or (lambda: None)
        self.on_event = on_event or (lambda *a, **k: None)

    # ----------------------------------------------------------- suite
    def run_suite(
        self, worktree: Path, plan: PlanResult, timeout: float = 600
    ) -> dict[str, Any]:
        """The TESTING stage: real test + build commands, once.

        Kept separate so the pipeline can put tests in the TESTING
        state and the full evidence audit in VERIFYING without paying
        for the suite twice.
        """
        checks = [
            *self._test_checks(worktree, plan, timeout),
            *self._build_checks(worktree, plan, timeout),
        ]
        return {
            "checks": checks,
            "tests_passed": self._sum(checks, "tests", "passed"),
            "tests_failed": self._sum(checks, "tests", "failed"),
            "build_status": self._named_status(checks, "build"),
            "tests_status": self._named_status(checks, "tests"),
        }

    # ------------------------------------------------------ pre-commit
    def verify_worktree(
        self,
        task: TaskRecord,
        plan: PlanResult,
        repo: Path,
        worktree: Path,
        timeout: float = 600,
        suite: Optional[dict[str, Any]] = None,
    ) -> VerificationReport:
        checks: list[VerificationCheck] = []

        checks.append(self._worktree_check(worktree, task.branch))
        changed, status_text = self._changed_files(worktree)
        checks.append(
            VerificationCheck(
                name="files_changed",
                status="PASS" if changed else "FAIL",
                detail=(
                    f"{len(changed)} changed file(s)"
                    if changed
                    else "the worktree has no changes at all"
                ),
                evidence={"files": changed[:50]},
            )
        )
        checks.append(self._expected_files_check(worktree, plan, changed))
        checks.append(self._exact_content_check(worktree, plan))
        checks.append(self._scope_check(plan, changed))
        checks.append(self._syntax_check(worktree, changed, timeout))
        if suite is not None:
            checks.extend(suite.get("checks") or [])
        else:
            checks.extend(self._test_checks(worktree, plan, timeout))
            checks.extend(self._build_checks(worktree, plan, timeout))
        checks.append(self._acceptance_check(plan, checks, changed))

        diff = self._diff(worktree)
        report = VerificationReport(
            status=self._status(checks),
            checks=checks,
            files_changed=len(changed),
            touched_files=changed,
            out_of_scope=self._out_of_scope(plan, changed),
            tests_passed=self._sum(checks, "tests", "passed"),
            tests_failed=self._sum(checks, "tests", "failed"),
            build_status=self._named_status(checks, "build"),
            diff=diff[:20000],
            detail=status_text[:500],
        )
        return report

    # ----------------------------------------------------- post-commit
    def verify_commit(
        self,
        task: TaskRecord,
        plan: PlanResult,
        repo: Path,
        worktree: Path,
        commit_sha: Optional[str],
        expected_files: list[str],
    ) -> VerificationCheck:
        """A commit must exist and must really contain the changes."""
        if not commit_sha:
            return VerificationCheck(
                name="commit",
                status="FAIL",
                detail="no commit was produced",
            )

        head = run(["git", "rev-parse", "HEAD"], cwd=str(worktree), timeout=60)
        head_sha = (head.get("stdout") or "").strip()
        if head.get("returncode") != 0 or head_sha != commit_sha:
            return VerificationCheck(
                name="commit",
                status="FAIL",
                detail=f"HEAD is {head_sha or 'unknown'}, expected {commit_sha}",
                evidence={"head": head_sha, "expected": commit_sha},
            )

        kind = run(["git", "cat-file", "-t", commit_sha], cwd=str(worktree), timeout=60)
        if (kind.get("stdout") or "").strip() != "commit":
            return VerificationCheck(
                name="commit",
                status="FAIL",
                detail="the recorded object is not a commit",
                evidence={"object_type": (kind.get("stdout") or "").strip()},
            )

        files_result = run(
            ["git", "show", "--name-only", "--pretty=format:", commit_sha],
            cwd=str(worktree),
            timeout=60,
        )
        committed = [
            line.strip()
            for line in (files_result.get("stdout") or "").splitlines()
            if line.strip()
        ]
        if not committed:
            return VerificationCheck(
                name="commit",
                status="FAIL",
                detail="the commit contains no file changes",
                evidence={"commit": commit_sha},
            )

        missing = [path for path in expected_files if path not in committed]
        if missing:
            return VerificationCheck(
                name="commit",
                status="FAIL",
                detail=f"commit is missing expected file(s): {missing}",
                evidence={"committed": committed[:50], "missing": missing},
            )

        tree_clean = run(["git", "status", "--porcelain"], cwd=str(worktree), timeout=60)
        clean = not (tree_clean.get("stdout") or "").strip()
        return VerificationCheck(
            name="commit",
            status="PASS",
            detail=f"commit {commit_sha[:10]} contains {len(committed)} file(s)",
            evidence={
                "commit": commit_sha,
                "files": committed[:50],
                "worktree_clean": clean,
            },
        )

    def merge(
        self, report: VerificationReport, commit_check: VerificationCheck
    ) -> VerificationReport:
        report.checks.append(commit_check)
        report.commit_sha = (
            commit_check.evidence.get("commit") if commit_check.status == "PASS" else None
        )
        report.status = self._status(report.checks)
        if report.status == "FAIL":
            failed = ", ".join(c.name for c in report.failed_checks())
            report.detail = (report.detail + f" | failed checks: {failed}").strip()
        return report

    # ---------------------------------------------------------- checks
    def _worktree_check(self, worktree: Path, branch: Optional[str]) -> VerificationCheck:
        if not worktree.exists():
            return VerificationCheck(
                name="worktree",
                status="FAIL",
                detail=f"worktree {worktree} does not exist",
            )
        git_file = worktree / ".git"
        if not git_file.exists():
            return VerificationCheck(
                name="worktree",
                status="FAIL",
                detail="worktree has no git metadata",
            )
        branch_result = run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(worktree), timeout=60
        )
        current = (branch_result.get("stdout") or "").strip()
        if branch and current and current != branch:
            return VerificationCheck(
                name="worktree",
                status="FAIL",
                detail=f"worktree is on {current}, expected {branch}",
            )
        return VerificationCheck(
            name="worktree",
            status="PASS",
            detail=f"isolated worktree on {current or branch or 'HEAD'}",
            evidence={"worktree": str(worktree), "branch": current or branch},
        )

    def _changed_files(self, worktree: Path) -> tuple[list[str], str]:
        result = run(["git", "status", "--porcelain"], cwd=str(worktree), timeout=60)
        text = result.get("stdout") or ""
        files: list[str] = []
        for line in text.splitlines():
            entry = line[3:].strip() if len(line) > 3 else line.strip()
            if " -> " in entry:
                entry = entry.split(" -> ")[-1].strip()
            if entry:
                files.append(entry.strip('"'))
        return files, text

    def _expected_files_check(
        self, worktree: Path, plan: PlanResult, changed: list[str]
    ) -> VerificationCheck:
        # Every deterministic create/edit is an expectation; so is
        # every declared target file.
        expected = set(plan.target_files)
        for edit in plan.edits:
            expected.add(edit.target_file)
        if not expected:
            return VerificationCheck(
                name="expected_files",
                status="PASS",
                detail="the plan declared no specific files",
                evidence={"expected": []},
            )
        missing = sorted(path for path in expected if not (worktree / path).exists())
        untouched = sorted(path for path in expected if path not in changed)
        if missing:
            return VerificationCheck(
                name="expected_files",
                status="FAIL",
                detail=f"planned file(s) do not exist: {missing}",
                evidence={"missing": missing, "expected": sorted(expected)},
            )
        if untouched:
            return VerificationCheck(
                name="expected_files",
                status="FAIL",
                detail=f"planned file(s) were never changed: {untouched}",
                evidence={"untouched": untouched, "expected": sorted(expected)},
            )
        return VerificationCheck(
            name="expected_files",
            status="PASS",
            detail=f"all {len(expected)} planned file(s) exist and changed",
            evidence={"expected": sorted(expected)},
        )

    def _exact_content_check(self, worktree: Path, plan: PlanResult) -> VerificationCheck:
        opaque = [edit for edit in plan.edits if edit.find == "" and edit.replace]
        if not opaque:
            return VerificationCheck(
                name="exact_content",
                status="SKIPPED",
                detail="the plan pinned no exact file content",
            )
        mismatches: list[dict[str, Any]] = []
        for edit in opaque:
            target = worktree / edit.target_file
            if not target.exists():
                mismatches.append(
                    {"file": edit.target_file, "reason": "missing"}
                )
                continue
            try:
                actual = target.read_text(errors="replace")
            except OSError as exc:
                mismatches.append(
                    {"file": edit.target_file, "reason": f"unreadable: {exc}"}
                )
                continue
            if actual != edit.replace:
                mismatches.append(
                    {
                        "file": edit.target_file,
                        "reason": "content mismatch",
                        "expected_bytes": len(edit.replace),
                        "actual_bytes": len(actual),
                    }
                )
        if mismatches:
            return VerificationCheck(
                name="exact_content",
                status="FAIL",
                detail=f"{len(mismatches)} file(s) do not match the required content",
                evidence={"mismatches": mismatches},
            )
        return VerificationCheck(
            name="exact_content",
            status="PASS",
            detail=f"{len(opaque)} file(s) match the required content exactly",
            evidence={"files": [edit.target_file for edit in opaque]},
        )

    def _scope_check(self, plan: PlanResult, changed: list[str]) -> VerificationCheck:
        out_of_scope = self._out_of_scope(plan, changed)
        if not plan.expected_scope and not plan.target_files and not plan.edits:
            return VerificationCheck(
                name="scope",
                status="SKIPPED",
                detail="the plan declared no scope to enforce",
            )
        if out_of_scope:
            return VerificationCheck(
                name="scope",
                status="FAIL",
                detail=f"changes outside the declared scope: {out_of_scope[:10]}",
                evidence={"out_of_scope": out_of_scope},
            )
        return VerificationCheck(
            name="scope",
            status="PASS",
            detail="every change is inside the declared scope",
        )

    def _out_of_scope(self, plan: PlanResult, changed: list[str]) -> list[str]:
        allowed = set(plan.expected_scope) | set(plan.target_files) | {
            edit.target_file for edit in plan.edits
        }
        if not allowed:
            return []
        out: list[str] = []
        for path in changed:
            if path in allowed:
                continue
            if any(
                path.startswith(prefix.rstrip("/") + "/") for prefix in allowed
            ):
                continue
            out.append(path)
        return out

    def _syntax_check(
        self, worktree: Path, changed: list[str], timeout: float
    ) -> VerificationCheck:
        python_files = [p for p in changed if p.endswith(".py")]
        js_files = [
            p for p in changed if p.endswith((".js", ".mjs", ".cjs"))
        ]
        evidence: dict[str, Any] = {"python": [], "javascript": []}

        if python_files:
            import sys

            result = run(
                [sys.executable, "-m", "py_compile", *python_files],
                cwd=str(worktree),
                timeout=min(timeout, 180),
            )
            evidence["python"] = [
                {
                    "returncode": result.get("returncode"),
                    "stderr": (result.get("stderr") or "")[:1000],
                }
            ]
            if result.get("returncode") != 0:
                return VerificationCheck(
                    name="syntax",
                    status="FAIL",
                    detail="python syntax check failed",
                    evidence=evidence,
                )

        if js_files:
            import shutil

            node = shutil.which("node")
            if node is None:
                evidence["javascript"] = "node not installed"
            else:
                for path in js_files[:20]:
                    result = run(
                        [node, "--check", path],
                        cwd=str(worktree),
                        timeout=min(timeout, 120),
                    )
                    evidence["javascript"].append(
                        {
                            "file": path,
                            "returncode": result.get("returncode"),
                            "stderr": (result.get("stderr") or "")[:400],
                        }
                    )
                    if result.get("returncode") != 0:
                        return VerificationCheck(
                            name="syntax",
                            status="FAIL",
                            detail=f"javascript syntax check failed: {path}",
                            evidence=evidence,
                        )

        if not python_files and not js_files:
            return VerificationCheck(
                name="syntax",
                status="SKIPPED",
                detail="no python/javascript files changed",
                evidence=evidence,
            )
        return VerificationCheck(
            name="syntax",
            status="PASS",
            detail="syntax checks passed for changed sources",
            evidence=evidence,
        )

    def _test_checks(
        self, worktree: Path, plan: PlanResult, timeout: float
    ) -> list[VerificationCheck]:
        from app.workers.validation import detect_test_commands

        try:
            commands = detect_test_commands(worktree)
        except ToolMissingError as exc:
            return [
                VerificationCheck(
                    name="tests",
                    status="FAIL",
                    detail=f"test tooling unavailable: {exc}",
                )
            ]
        if not commands:
            return [
                VerificationCheck(
                    name="tests",
                    status="SKIPPED",
                    detail="no test runner detected in this repository",
                )
            ]

        checks: list[VerificationCheck] = []
        for cmd in commands:
            self.cancel_check()
            self.on_event(
                "test.started", {"command": " ".join(cmd) if isinstance(cmd, list) else str(cmd)}
            )
            try:
                result = run(
                    cmd,
                    cwd=str(worktree),
                    timeout=timeout,
                    cancel_check=self.cancel_check,
                )
            except FileNotFoundError as exc:
                checks.append(
                    VerificationCheck(
                        name="tests",
                        status="FAIL",
                        detail=f"test tool could not be spawned: {exc}",
                        evidence={"command": cmd},
                    )
                )
                continue
            passed, failed = parse_test_counts(
                (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
            )
            ok = result.get("returncode") == 0 and not result.get("timed_out")
            self.on_event(
                "test.completed",
                {
                    "command": result.get("cmd"),
                    "returncode": result.get("returncode"),
                    "passed": passed,
                    "failed": failed,
                    "status": "PASS" if ok else "FAIL",
                },
            )
            checks.append(
                VerificationCheck(
                    name="tests",
                    status="PASS" if ok else "FAIL",
                    detail=(
                        f"{result.get('cmd')} exited "
                        f"{result.get('returncode')}"
                        + (" (timed out)" if result.get("timed_out") else "")
                    ),
                    evidence={
                        "command": result.get("cmd"),
                        "returncode": result.get("returncode"),
                        "timed_out": bool(result.get("timed_out")),
                        "passed": passed,
                        "failed": failed,
                        "output_tail": (
                            (result.get("stdout") or "")[-1500:]
                        ),
                    },
                )
            )
        return checks

    def _build_checks(
        self, worktree: Path, plan: PlanResult, timeout: float
    ) -> list[VerificationCheck]:
        """Run the build the plan promised, when one is identifiable."""
        checks: list[VerificationCheck] = []
        if plan.build_command:
            checks.append(
                self._run_named_command(
                    "build", plan.build_command, worktree, timeout
                )
            )
            return checks

        # No explicit build, but the repository declares one.
        package = worktree / "package.json"
        if package.is_file():
            try:
                scripts = json.loads(package.read_text(errors="replace")).get(
                    "scripts"
                ) or {}
            except Exception:
                scripts = {}
            if scripts.get("build"):
                checks.append(
                    self._run_named_command(
                        "build", "npm run build", worktree, timeout
                    )
                )
                return checks
        checks.append(
            VerificationCheck(
                name="build",
                status="SKIPPED",
                detail="the plan declared no build step",
            )
        )
        return checks

    def _run_named_command(
        self, name: str, command: str, worktree: Path, timeout: float
    ) -> VerificationCheck:
        import shlex

        self.on_event(f"{name}.started", {"command": command})
        try:
            result = run(
                shlex.split(command),
                cwd=str(worktree),
                timeout=timeout,
                cancel_check=self.cancel_check,
            )
        except FileNotFoundError as exc:
            return VerificationCheck(
                name=name,
                status="FAIL",
                detail=f"{command} could not be spawned: {exc}",
            )
        ok = result.get("returncode") == 0 and not result.get("timed_out")
        self.on_event(
            f"{name}.completed",
            {"command": command, "status": "PASS" if ok else "FAIL"},
        )
        return VerificationCheck(
            name=name,
            status="PASS" if ok else "FAIL",
            detail=(
                f"{command} exited {result.get('returncode')}"
                + (" (timed out)" if result.get("timed_out") else "")
            ),
            evidence={
                "command": command,
                "returncode": result.get("returncode"),
                "output_tail": (result.get("stdout") or "")[-1500:],
            },
        )

    def _acceptance_check(
        self,
        plan: PlanResult,
        checks: list[VerificationCheck],
        changed: list[str],
    ) -> VerificationCheck:
        """Report acceptance criteria against the evidence collected.

        A criterion is recorded PENDING unless it maps onto a check that
        actually ran; nothing is marked satisfied on the strength of the
        executor's word.
        """
        rendered: list[dict[str, str]] = []
        evidence_failed = any(check.status == "FAIL" for check in checks)
        for item in plan.acceptance:
            lowered = item.lower()
            if ("build" in lowered) and self._named_status(checks, "build") == "PASS":
                rendered.append({"criterion": item, "status": "PASS"})
            elif ("test" in lowered) and self._named_status(checks, "tests") == "PASS":
                rendered.append({"criterion": item, "status": "PASS"})
            elif "no unrelated" in lowered or "scope" in lowered:
                scope = self._named_status(checks, "scope")
                rendered.append(
                    {
                        "criterion": item,
                        "status": "PASS" if scope in ("PASS", "SKIPPED") else "FAIL",
                    }
                )
            elif ("implemented" in lowered or "exists" in lowered or "created" in lowered):
                rendered.append(
                    {
                        "criterion": item,
                        "status": "PASS" if changed and not evidence_failed else "PENDING",
                    }
                )
            else:
                rendered.append({"criterion": item, "status": "PENDING"})
        failed = [entry for entry in rendered if entry["status"] == "FAIL"]
        return VerificationCheck(
            name="acceptance",
            status="FAIL" if failed else "PASS",
            detail=(
                f"{sum(1 for e in rendered if e['status'] == 'PASS')}/"
                f"{len(rendered)} criteria backed by real evidence"
            ),
            evidence={"criteria": rendered},
        )

    # -------------------------------------------------------- helpers
    def _status(self, checks: list[VerificationCheck]) -> str:
        if any(check.status == "FAIL" for check in checks):
            return "FAIL"
        if not self._mandatory_satisfied(checks):
            return "FAIL"
        return "PASS"

    def _mandatory_satisfied(self, checks: list[VerificationCheck]) -> bool:
        by_name = {check.name: check.status for check in checks}
        for name in MANDATORY_CHECKS:
            if by_name.get(name) == "FAIL":
                return False
        # files_changed must have actually been evaluated and passed.
        return by_name.get("files_changed") == "PASS" and by_name.get(
            "worktree"
        ) == "PASS"

    def _sum(self, checks: list[VerificationCheck], name: str, key: str) -> int:
        total = 0
        for check in checks:
            if check.name == name:
                total += int(check.evidence.get(key) or 0)
        return total

    def _named_status(self, checks: list[VerificationCheck], name: str) -> str:
        statuses = [check.status for check in checks if check.name == name]
        if not statuses:
            return "SKIPPED"
        if "FAIL" in statuses:
            return "FAIL"
        if all(status == "SKIPPED" for status in statuses):
            return "SKIPPED"
        return "PASS"

    def _diff(self, worktree: Path) -> str:
        run(["git", "add", "-A"], cwd=str(worktree), timeout=120)
        result = run(["git", "diff", "--cached"], cwd=str(worktree), timeout=120)
        return result.get("stdout") or ""


def parse_test_counts(text: str) -> tuple[int, int]:
    """Extract ``(passed, failed)`` from pytest/jest-style output.

    Returns ``(0, 0)`` when the output carries no counts — the caller
    then relies on the exit code, never on a fabricated number.
    """
    if not text:
        return 0, 0
    match = PYTEST_COUNT.search(text)
    if match:
        return int(match.group("passed")), int(match.group("failed") or 0)
    match = JEST_COUNT.search(text)
    if match:
        return int(match.group("passed")), int(match.group("failed"))
    match = JEST_ALL.search(text)
    if match:
        return int(match.group("passed")), 0
    match = PYTEST_FAILED.search(text)
    if match:
        return 0, int(match.group("failed"))
    return 0, 0
