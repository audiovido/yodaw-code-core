"""Slash-command implementations for the interactive shell."""

from __future__ import annotations

from typing import Any

from app.cli.pipeline import collect_diff, rank_relevant_files
from app.cli.redact import redact_text
from app.cli.repo import RepoInfo
from app.cli.session import Session

SLASH_COMMANDS = (
    "/status",
    "/plan",
    "/diff",
    "/tests",
    "/evidence",
    "/model",
    "/provider",
    "/context",
    "/repo",
    "/history",
    "/resume",
    "/cancel",
    "/clear",
    "/approval",
    "/help",
    "/exit",
)

HELP_TEXT = """Commands:
  /status            session, repo, and current task state
  /plan              show the latest plan steps
  /diff [--full]     changed files plus a patch preview
  /tests             discovered test files for this repo
  /evidence [N]      recent evidence entries
  /model [name]      show or set the model (auto clears)
  /provider [name]   show or set the provider style
  /context           repo, languages, relevant files, budget
  /repo              repository identity and dirty state
  /history [N]       recent conversation turns
  /resume [id]       resume a persisted session
  /cancel            cancel the current task
  /clear             clear visible conversation (history kept on disk)
  /approval [mode]   show or set safe|standard|auto
  /help              this text
  /exit              leave the shell (session is saved)
Any other line is a natural-language task; 'cancel' and 'exit' also work bare.
"""


def is_slash_command(line: str) -> bool:
    """True when a line is a slash command rather than a task prompt."""
    text = line.strip().lower()
    if text.startswith("/"):
        return True
    return text in {"status", "history", "resume", "cancel", "clear", "help", "exit"}


def _history_lines(session: Session, limit: int = 10) -> list[str]:
    lines = []
    for turn in session.history[-limit:]:
        role = turn.get("role", "?")
        text = redact_text(str(turn.get("text", "")))[:300]
        lines.append(f"{role}: {text}")
    return lines or ["no conversation yet"]


def cmd_status(session: Session, repo: RepoInfo) -> str:
    """Session, repository, and current-task summary."""
    current = next((t for t in session.tasks if t.task_id == session.current_task_id), None)
    lines = [
        f"session: {session.session_id} (started {session.started_at})",
        f"repo: {repo.root} branch={repo.branch or '?'} {'dirty' if repo.dirty else 'clean'}",
        f"tasks: {len(session.tasks)} turns: {len(session.history)} evidence: {len(session.evidence)}",
        f"model: {session.model or 'auto'} provider: {session.provider or 'auto'} approval: {session.approval_mode}",
    ]
    if current:
        lines.append(f"current task: {current.task_id} goal={redact_text(current.goal)[:120]} status={current.status}")
    else:
        lines.append("current task: none")
    return "\n".join(lines)


def cmd_plan(session: Session) -> str:
    """Latest plan progress with step counts."""
    progress = session.plan_progress or {}
    if not progress:
        return "no plan yet; describe a task first"
    total = progress.get("total", 0)
    done = len(progress.get("completed", []))
    return f"plan {progress.get('plan_id', '?')}: {done}/{total} steps complete"


def cmd_diff(session: Session, repo: RepoInfo, full: bool = False) -> str:
    """Changed files with a bounded patch preview."""
    changed, preview = collect_diff(repo.root)
    if not changed:
        return "no local modifications detected"
    lines = [f"changed files ({len(changed)}):"]
    shown = changed if full else changed[:20]
    lines.extend(f"  {item}" for item in shown)
    if not full and len(changed) > 20:
        lines.append(f"  ... and {len(changed) - 20} more (use /diff --full)")
    if preview:
        lines.append("patch preview:")
        lines.append(preview if full else preview[:4000])
    return "\n".join(lines)


def cmd_tests(repo: RepoInfo) -> str:
    """Discovered test files without dumping full repo maps."""
    if repo.root is None:
        return "no repository context"
    try:
        from app.repo_intelligence.tests_discovery import discover_tests
        from app.repo_intelligence.detector import list_repo_files
        nodes, _, _ = list_repo_files(repo.root)
        tests = discover_tests([node.path for node in nodes])[:30]
    except Exception as exc:
        return f"test discovery unavailable: {exc}"
    if not tests:
        return "no test files discovered"
    return "tests:\n" + "\n".join(f"  {path}" for path in tests)


def cmd_evidence(session: Session, limit: int = 5) -> str:
    """Recent evidence entries with secret values redacted."""
    if not session.evidence:
        return "no evidence yet"
    lines = [f"evidence (last {min(limit, len(session.evidence))} of {len(session.evidence)}):"]
    for entry in session.evidence[-limit:]:
        etype = entry.get("type", "?")
        lines.append(f"  - {redact_text(str(etype))}")
    return "\n".join(lines)


def cmd_context(session: Session, repo: RepoInfo, goal: str = "") -> str:
    """Concise working context: repo, languages, relevant files, budget."""
    lines = [
        f"repo: {repo.root} ({repo.repo_type}) branch={repo.branch or '?'}",
        f"files: {repo.file_count} budget: context window bounded; relevance ranking top 8",
    ]
    if repo.languages:
        top = ", ".join(f"{lang} {count}" for lang, count in sorted(repo.languages.items())[:5])
        lines.append(f"languages: {top}")
    if goal and repo.root is not None:
        try:
            relevant = rank_relevant_files(goal or "current task", repo.root, top_n=5)
            if relevant:
                lines.append("relevant files:")
                lines.extend(f"  {item['path']} (score {item['score']:.1f})" for item in relevant)
        except Exception as exc:
            lines.append(f"relevance unavailable: {exc}")
    lines.append(f"approval: {session.approval_mode} model: {session.model or 'auto'}")
    return "\n".join(lines)


def cmd_model(session: Session, arg: str = "") -> str:
    """Show or set the model; never hardcodes provider business logic."""
    if arg:
        session.model = None if arg == "auto" else arg
        return f"model: {session.model or 'auto'}"
    return f"model: {session.model or 'auto'} provider: {session.provider or 'auto'}"


def cmd_provider(session: Session, arg: str = "") -> str:
    """Show or set the provider style label used by the routing adapter."""
    if arg:
        session.provider = None if arg == "auto" else arg
        return f"provider: {session.provider or 'auto'}"
    return f"provider: {session.provider or 'auto'}"


def dispatch(line: str, session: Session, repo: RepoInfo) -> tuple[str, bool, dict[str, Any] | None]:
    """Run one slash command; returns (output, should_exit, action)."""
    parts = line.strip().split()
    if not parts:
        return "", False, None
    name = parts[0].lower().lstrip("/")
    args = parts[1:]
    if name == "status":
        return cmd_status(session, repo), False, None
    if name == "plan":
        return cmd_plan(session), False, None
    if name == "diff":
        return cmd_diff(session, repo, full="--full" in args), False, None
    if name == "tests":
        return cmd_tests(repo), False, None
    if name == "evidence":
        limit = int(args[0]) if args and args[0].isdigit() else 5
        return cmd_evidence(session, limit), False, None
    if name == "model":
        return cmd_model(session, args[0] if args else ""), False, None
    if name == "provider":
        return cmd_provider(session, args[0] if args else ""), False, None
    if name == "context":
        return cmd_context(session, repo), False, None
    if name == "repo":
        state = "dirty" if repo.dirty else "clean"
        return f"repo: {repo.root}\nbranch: {repo.branch or 'unknown'} ({state})\ntype: {repo.repo_type}", False, None
    if name == "history":
        limit = int(args[0]) if args and args[0].isdigit() else 10
        return "\n".join(_history_lines(session, limit)), False, None
    if name == "resume":
        return "", False, {"resume": args[0] if args else None}
    if name == "cancel":
        return "", False, {"cancel": True}
    if name == "clear":
        return "", False, {"clear": True}
    if name == "approval":
        if args and args[0] in ("safe", "standard", "auto"):
            session.approval_mode = args[0]
            return f"approval: {session.approval_mode}", False, None
        return f"approval: {session.approval_mode} (safe|standard|auto)", False, None
    if name == "help":
        return HELP_TEXT, False, None
    if name in {"exit", "quit"}:
        return "bye", True, None
    return f"unknown command: {parts[0]} (try /help)", False, None
