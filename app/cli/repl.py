"""Persistent interactive shell: history, safe interrupts, live events."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from app.cli import __version__ as cli_version
from app.cli.commands import dispatch, is_slash_command
from app.cli.events import render_human, supports_color
from app.cli.pipeline import PipelineResult, run_task
from app.cli.redact import redact_text
from app.cli.repo import RepoInfo, detect_repo, startup_lines
from app.cli.session import Session, latest_session_id, load_session, save_session

PROMPT = "YODAW > "


class Repl:
    """Interactive loop owning input, output, and session lifecycle."""

    def __init__(
        self,
        repo: RepoInfo,
        session: Session,
        approval_mode: str = "standard",
        verbose: bool = False,
        json_mode: bool = False,
        output: Any = None,
        input_func: Callable[[str], str] | None = None,
        confirm_func: Callable[[str], bool] | None = None,
        executor=None,
    ) -> None:
        self.repo = repo
        self.session = session
        self.session.approval_mode = approval_mode
        self.verbose = verbose
        self.json_mode = json_mode
        self.output = output or sys.stdout
        self.input_func = input_func or (lambda prompt: input(prompt))
        self.confirm_func = confirm_func
        self.executor = executor
        self.color = supports_color(self.output) and not json_mode
        self._cancel_pending = False
        self._setup_history()

    def _setup_history(self) -> None:
        try:
            import readline  # noqa: F401
        except ImportError:
            return
        try:
            import readline
            histfile = Path.home() / ".yodaw" / "history"
            histfile.parent.mkdir(parents=True, exist_ok=True)
            if histfile.exists():
                readline.read_history_file(str(histfile))
            readline.set_history_length(500)
            self._histfile = str(histfile)
        except (OSError, AttributeError):
            self._histfile = None

    def _save_history(self) -> None:
        histfile = getattr(self, "_histfile", None)
        if not histfile:
            return
        try:
            import readline
            readline.write_history_file(histfile)
        except (OSError, AttributeError):
            pass

    def emit(self, text: str) -> None:
        self.output.write(redact_text(text) + "\n")
        self.output.flush()

    def emit_events(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            if self.json_mode:
                import json
                from app.cli.redact import redact_mapping
                self.output.write(json.dumps(redact_mapping(dict(event)), default=str) + "\n")
            else:
                self.emit(render_human(event, verbose=self.verbose, color=self.color))
        self.output.flush()

    def confirm(self, question: str) -> bool:
        """Approval prompt; safe mode always asks, others defer to caller."""
        if self.confirm_func is not None:
            return bool(self.confirm_func(question))
        isatty = getattr(self.output, "isatty", None)
        if callable(isatty) and not isatty():
            return False
        try:
            answer = self.input_func(f"{question} [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in {"y", "yes"}

    def show_banner(self) -> None:
        if self.json_mode:
            return
        self.emit(f"YODAW native shell v{cli_version} (approval: {self.session.approval_mode})")
        for line in startup_lines(self.repo):
            self.emit(line)
        self.emit("type /help for commands; Ctrl+C interrupts safely, Ctrl+D exits")

    def handle_line(self, line: str) -> bool:
        """Process one input line; returns True when the shell should exit."""
        stripped = line.strip()
        if not stripped:
            return False
        lowered = stripped.lower()
        if lowered in {"exit", "quit", "/exit", "/quit"}:
            self.emit("bye")
            return True
        if lowered in {"cancel", "/cancel"}:
            self._cancel_pending = True
            self.session.history.append({"role": "system", "text": "current task cancelled"})
            self.emit("cancelled current task")
            return False
        if lowered in {"clear", "/clear"}:
            self.emit("(conversation cleared)")
            return False
        if is_slash_command(stripped):
            output, should_exit, action = dispatch(stripped, self.session, self.repo)
            if action and "resume" in action:
                self._resume_into(action["resume"])
                return False
            if output:
                self.emit(output)
            save_session(self.session)
            return should_exit
        return self._run_goal(stripped)

    def _resume_into(self, session_id: str | None) -> None:
        target = session_id or latest_session_id()
        if not target:
            self.emit("no saved sessions to resume")
            return
        if target == self.session.session_id:
            self.emit(f"already in session {target}")
            return
        loaded = load_session(target)
        if loaded is None:
            self.emit(f"cannot resume session {target}")
            return
        self.session = loaded
        self.emit(f"resumed session {loaded.session_id} ({len(loaded.history)} turns, {len(loaded.tasks)} tasks)")

    def _run_goal(self, goal: str) -> bool:
        self.session.history.append({"role": "user", "text": goal})
        approved = self.session.approval_mode == "auto"
        if self.session.approval_mode == "safe":
            approved = self.confirm(f"Run task: {goal}?")
        try:
            result: PipelineResult = run_task(
                goal,
                self.repo,
                self.session,
                approval_mode=self.session.approval_mode,
                approved=approved,
                confirm=self.confirm if self.session.approval_mode != "auto" else None,
                executor=self.executor,
            )
        except KeyboardInterrupt:
            self.session.history.append({"role": "system", "text": "task interrupted; session preserved"})
            save_session(self.session)
            self.emit("interrupted; session preserved — describe the next step when ready")
            return False
        self.emit_events(result.events)
        if result.summary and not self.json_mode:
            self.emit(result.summary)
        self.session.history.append({"role": "assistant", "text": f"[{result.status}] {result.summary}"[:2000]})
        self.session.evidence.extend(result.evidence[-25:])
        save_session(self.session)
        return False

    def run(self) -> int:
        """Main loop: never crash the session on a single failed task."""
        self.show_banner()
        if self._cancel_pending:
            self._cancel_pending = False
        while True:
            try:
                try:
                    line = self.input_func(PROMPT)
                except EOFError:
                    self.emit("bye")
                    break
                except OSError:
                    # Closed or unreadable stdin (pipes, CI):
                    # exit quietly instead of error-looping.
                    break
                if self.handle_line(line):
                    break
            except KeyboardInterrupt:
                self.session.history.append({"role": "system", "text": "interrupted; session preserved"})
                save_session(self.session)
                self.emit("interrupted; session preserved — describe the next step when ready")
                continue
            except StopIteration:
                self.emit("bye")
                break
            except Exception as exc:
                self.emit(f"shell error (session preserved): {exc}")
                try:
                    save_session(self.session)
                except OSError:
                    pass
                continue
        self._save_history()
        try:
            save_session(self.session)
        except OSError:
            pass
        return 0


def start_interactive(
    repo_path: str | None = None,
    resume_id: str | None = None,
    approval_mode: str = "standard",
    verbose: bool = False,
    json_mode: bool = False,
    model: str | None = None,
    provider: str | None = None,
) -> int:
    """Launch the REPL, optionally resuming a persisted session."""
    repo = detect_repo(repo_path or ".")
    session: Session | None = load_session(resume_id) if resume_id else None
    if resume_id and session is None:
        print(f"cannot resume session {resume_id}", file=sys.stderr)
        return 2
    if session is None:
        session = Session(
            repo=str(repo.root) if repo.root else None,
            branch=repo.branch,
            approval_mode=approval_mode,
            model=model,
            provider=provider,
        )
    else:
        session.approval_mode = approval_mode or session.approval_mode
        if model:
            session.model = model
        if provider:
            session.provider = provider
    repl = Repl(repo, session, approval_mode=session.approval_mode, verbose=verbose, json_mode=json_mode)
    return repl.run()
