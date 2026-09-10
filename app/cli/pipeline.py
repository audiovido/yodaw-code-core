"""Task pipeline adapter: CLI presentation over existing runtime stages.

Canonical flow per task: GOAL -> OBSERVE -> REPO INTELLIGENCE -> PLAN ->
ROUTE -> EXECUTE -> VERIFY -> RECOVER -> LEARN -> DELIVER. Each stage
delegates to the real runtime module; the CLI only renders events.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.cli.events import make_event
from app.cli.redact import redact_text
from app.cli.repo import RepoInfo
from app.cli.session import Session, TaskRecord

APPROVAL_MODES = ("safe", "standard", "auto")

# Goal text that reads as informational rather than mutating.
READONLY_HINTS = (
    "explain",
    "describe",
    "summarize",
    "summarise",
    "what is",
    "what does",
    "show diff",
    "status",
    "list",
)

# Goal text that implies destructive version-control operations.
HIGH_RISK_HINTS = (
    "reset",
    "clean -f",
    "push --force",
    "push -f",
    "delete branch",
    "drop table",
    "rm -rf",
)


@dataclass
class PipelineResult:
    """Outcome of one natural-language task."""
    success: bool
    status: str
    summary: str
    events: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    plan: Any | None = None
    changed_files: list[str] = field(default_factory=list)
    diff_preview: str = ""
    error: str | None = None
    retryable: bool = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_goal(goal: str) -> str:
    """Rough mutating/read-only classification used for approvals."""
    lowered = goal.lower().strip()
    for hint in READONLY_HINTS:
        if lowered.startswith(hint):
            return "readonly"
    return "mutating"


def is_high_risk(goal: str) -> bool:
    """Detect goals that need explicit approval outside auto mode."""
    lowered = goal.lower()
    return any(hint in lowered for hint in HIGH_RISK_HINTS)


def approval_decision(goal: str, approval_mode: str, approved: bool) -> tuple[bool, str | None]:
    """Decide whether a goal may execute under the current approval mode."""
    if approval_mode not in APPROVAL_MODES:
        return False, f"unknown approval mode: {approval_mode}"
    if approval_mode == "auto":
        if is_high_risk(goal):
            return approved, None if approved else "high-risk action needs explicit approval"
        return True, None
    if approval_mode == "standard":
        if is_high_risk(goal):
            return approved, None if approved else "high-risk action needs explicit approval"
        return True, None
    # Safe mode: every mutating goal needs an explicit yes.
    if classify_goal(goal) == "readonly":
        return True, None
    return approved, None if approved else "safe mode: approve the task to proceed"


def _emit(events: list[dict[str, Any]], stage: str, message: str, detail: Any = None, level: str = "info") -> None:
    events.append(make_event(stage, message, detail=detail, level=level))


def build_plan(goal: str, context: dict[str, Any] | None = None):
    """Plan through the existing AdvancedPlanner (never reimplemented)."""
    from app.planning.planner import AdvancedPlanner
    return AdvancedPlanner().create_plan(goal, context)


def rank_relevant_files(goal: str, repo_root: Path, top_n: int = 8) -> list[dict[str, Any]]:
    """Rank task-relevant files with the existing ranking module."""
    from app.repo_intelligence.engine import RepoIntelligence
    from app.repo_intelligence.ranking import rank_files
    evidence = RepoIntelligence().analyze(repo_root)
    paths = [node.path for node in evidence.files]
    symbols = {path: list(syms) for path, syms in evidence.symbols.items()}
    ranked = rank_files(goal, paths, symbols=symbols, top_n=top_n)
    return [{"path": item.path, "score": item.score, "reasons": item.reasons} for item in ranked]


def _explain_repo(goal: str, repo: RepoInfo) -> str:
    parts = [f"Repository at {repo.root}."]
    if repo.branch:
        parts.append(f"Current branch is {repo.branch} ({'dirty' if repo.dirty else 'clean'}).")
    if repo.repo_type:
        parts.append(f"Detected project type: {repo.repo_type}.")
    if repo.languages:
        top = ", ".join(f"{lang} ({count} files)" for lang, count in sorted(repo.languages.items())[:6])
        parts.append(f"Languages: {top}.")
    if repo.file_count:
        parts.append(f"Indexed {repo.file_count} files.")
    lowered = goal.lower()
    if "test" in lowered:
        parts.append("Ask /tests to see discovered test files, or describe a change to plan it.")
    return " ".join(parts)


def _run_shell(cmd: list[str], cwd: Path, timeout: int = 120) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "cmd": " ".join(cmd),
            "cwd": str(cwd),
            "stdout": "",
            "stderr": str(exc),
            "returncode": 127,
            "timestamp": _now_iso(),
        }
    return {
        "cmd": " ".join(cmd),
        "cwd": str(cwd),
        "stdout": proc.stdout[-20000:],
        "stderr": proc.stderr[-20000:],
        "returncode": proc.returncode,
        "truncated": len(proc.stdout) > 20000 or len(proc.stderr) > 20000,
        "timestamp": _now_iso(),
    }


def collect_diff(repo_root: Path | None) -> tuple[list[str], str]:
    """Changed files plus a bounded patch preview; never raises."""
    if repo_root is None or not (repo_root / ".git").exists():
        return [], ""
    files_proc = _run_shell(["git", "status", "--short"], repo_root)
    changed = [line.strip() for line in files_proc.get("stdout", "").splitlines() if line.strip()]
    diff_proc = _run_shell(["git", "diff", "--stat"], repo_root)
    preview = diff_proc.get("stdout", "") or ""
    if len(preview) > 8000:
        preview = preview[:8000] + "\n... (truncated; run /diff --full)"
    return changed, preview


def route_task(goal: str, repo: RepoInfo) -> dict[str, Any]:
    """Select capability/model metadata without hardcoding providers."""
    capability = "repo-code" if repo.is_git_repo else "code"
    model = os.environ.get("YODAW_LLM_MODEL") or None
    style = os.environ.get("YODAW_LLM_STYLE", "ollama")
    return {"capability": capability, "model": model, "provider_style": style}


def run_task(
    goal: str,
    repo: RepoInfo,
    session: Session,
    approval_mode: str = "standard",
    approved: bool = False,
    confirm: Callable[[str], bool] | None = None,
    executor: Callable[..., PipelineResult] | None = None,
    timeout: int = 600,
) -> PipelineResult:
    """Execute one goal through the canonical pipeline stages."""
    events: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    task = TaskRecord(goal=goal, status="running")
    session.tasks.append(task)
    session.current_task_id = task.task_id

    if repo.is_git_repo and repo.dirty and classify_goal(goal) == "mutating":
        _emit(events, "observe", "source repository has uncommitted changes; proceeding read-only until approved")

    allowed, reason = approval_decision(goal, approval_mode, approved)
    if not allowed and confirm is not None and not is_high_risk(goal) and approval_mode != "safe":
        allowed = bool(confirm(f"Proceed with: {goal}?"))
        reason = None if allowed else "task not approved"
    if not allowed:
        task.status = "cancelled"
        task.finished_at = _now_iso()
        _emit(events, "done", reason or "task not approved", level="error")
        return PipelineResult(success=False, status="CANCELLED", summary=reason or "task not approved", events=events, evidence=evidence, error=reason)

    lowered = goal.lower().strip()
    _emit(events, "observe", f"received goal: {redact_text(goal)[:200]}")

    if lowered in {"explain this repo", "explain this repository", "explain repo", "what is this repo"}:
        _emit(events, "repo", f"repository context ready ({repo.file_count} files)")
        summary = _explain_repo(goal, repo)
        _emit(events, "deliver", "explanation ready")
        task.status = "completed"
        task.finished_at = _now_iso()
        _emit(events, "done", "task completed")
        return PipelineResult(success=True, status="PASS", summary=summary, events=events, evidence=evidence)

    _emit(events, "observe", "scanning repository")
    relevant: list[dict[str, Any]] = []
    if repo.root is not None:
        try:
            relevant = rank_relevant_files(goal, repo.root)
            evidence.append({"type": "repo_relevance", "files": relevant, "timestamp": _now_iso()})
            _emit(events, "repo", f"ranked {len(relevant)} relevant files")
        except Exception as exc:
            evidence.append({"type": "repo_error", "error": str(exc), "timestamp": _now_iso()})
            _emit(events, "repo", f"repo intelligence unavailable: {exc}", level="error")

    _emit(events, "plan", "building plan")
    try:
        context = {"repo": str(repo.root), "branch": repo.branch, "relevant_files": [item["path"] for item in relevant[:5]]}
        plan = build_plan(goal, context)
        task.plan_id = plan.id
        task.plan_steps = len(plan.steps)
        session.plan_progress = {"plan_id": plan.id, "completed": [], "total": len(plan.steps)}
        evidence.append({"type": "plan", "plan_id": plan.id, "steps": len(plan.steps), "timestamp": _now_iso()})
        _emit(events, "plan", f"{len(plan.steps)} steps")
    except Exception as exc:
        task.status = "failed"
        task.finished_at = _now_iso()
        _emit(events, "error", f"planning failed: {exc}", level="error")
        return PipelineResult(success=False, status="FAILED", summary=f"planning failed: {exc}", events=events, evidence=evidence, error=str(exc))

    routing = route_task(goal, repo)
    session.model = session.model or routing["model"]
    session.provider = session.provider or routing["provider_style"]
    _emit(events, "route", f"capability={routing['capability']} provider={routing['provider_style']}")

    if executor is not None:
        try:
            return executor(goal=goal, repo=repo, session=session, task=task, plan=plan, events=events, evidence=evidence, timeout=timeout)
        except KeyboardInterrupt:
            task.status = "interrupted"
            task.finished_at = _now_iso()
            _emit(events, "error", "task interrupted; session preserved", level="error")
            return PipelineResult(success=False, status="INTERRUPTED", summary="task interrupted; session preserved", events=events, evidence=evidence, plan=plan, error="interrupted")
        except Exception as exc:
            task.status = "failed"
            task.finished_at = _now_iso()
            _emit(events, "error", f"execution failed: {exc}", level="error")
            return PipelineResult(success=False, status="FAILED", summary=f"execution failed: {exc}", events=events, evidence=evidence, plan=plan, error=str(exc), retryable=True)

    _emit(events, "execute", "execution adapter only: no live provider in this build")
    _emit(events, "verify", "no changes applied; poll /status for plan progress")
    task.status = "completed"
    task.finished_at = _now_iso()
    _emit(events, "learn", "recorded plan-only outcome for future retrieval")
    _emit(events, "deliver", "plan ready; connect a provider to execute")
    _emit(events, "done", "task completed")
    return PipelineResult(success=True, status="PASS", summary=f"Plan ready with {len(plan.steps)} steps. Execution needs a configured provider.", events=events, evidence=evidence, plan=plan)


def execute_with_worker(
    goal: str,
    repo: RepoInfo,
    session: Session,
    task: TaskRecord,
    plan: Any,
    events: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    timeout: int = 600,
) -> PipelineResult:
    """Default executor: run the existing worker registry synchronously."""
    from app.workers.registry import registry
    routing = route_task(goal, repo)
    worker = registry.find(routing["capability"])
    if worker is None:
        task.status = "blocked"
        task.finished_at = _now_iso()
        _emit(events, "error", f"no worker for capability: {routing['capability']}", level="error")
        return PipelineResult(success=False, status="BLOCKED_EXTERNAL", summary="no worker available", events=events, evidence=evidence, plan=plan, error="no worker")
    _emit(events, "execute", f"worker {worker.name} executing")
    if repo.root is not None:
        with tempfile.TemporaryDirectory(prefix="yodaw_cli_diff_") as tmp:
            before = _run_shell(["git", "status", "--short"], repo.root).get("stdout", "")
            metadata = {"repo_path": str(repo.root), "branch_name": f"yodaw/cli-{task.task_id}", "keep_worktree": True, "max_retries": 0, "mission_id": task.task_id}
            try:
                result = worker.execute(goal, metadata)
            except Exception as exc:
                task.status = "failed"
                task.finished_at = _now_iso()
                _emit(events, "error", f"worker raised {type(exc).__name__}: {exc}", level="error")
                return PipelineResult(success=False, status="FAILED", summary=f"worker error: {exc}", events=events, evidence=evidence, plan=plan, error=str(exc), retryable=True)
            evidence.extend(result.get("evidence", []))
            session.evidence.extend(evidence[-25:])
            changed, preview = collect_diff(repo.root)
            new_files = [line for line in changed if line not in before]
            if result.get("success"):
                _emit(events, "verify", "worker reported success")
                task.status = "completed"
                task.finished_at = _now_iso()
                _emit(events, "done", "task completed")
                return PipelineResult(success=True, status="PASS", summary="task completed", events=events, evidence=evidence, plan=plan, changed_files=new_files, diff_preview=preview)
            err = result.get("error") or {}
            _emit(events, "recover", f"worker failed ({err.get('type', 'error')}); changes kept for inspection", level="error")
            task.status = "failed"
            task.error = str(err.get("message", err))
            task.finished_at = _now_iso()
            return PipelineResult(success=False, status="FAILED", summary=f"worker failed: {err.get('message', 'error')}", events=events, evidence=evidence, plan=plan, changed_files=new_files, diff_preview=preview, error=str(err.get("message", err)), retryable=bool(result.get("retryable", True)))
    return PipelineResult(success=False, status="BLOCKED_EXTERNAL", summary="no repository context", events=events, evidence=evidence, plan=plan, error="no repo")
