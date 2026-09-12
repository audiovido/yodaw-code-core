"""Command entrypoint: `yodaw` interactive shell plus automation verbs."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from app.cli import __version__ as cli_version
from app.cli.pipeline import APPROVAL_MODES, PipelineResult, run_task
from app.cli.redact import redact_mapping
from app.cli.repo import detect_repo
from app.cli.session import (
    Session,
    latest_session_id,
    list_sessions,
    load_session,
    save_session,
)

EXIT_OK = 0
EXIT_TASK_FAILURE = 1
EXIT_USAGE = 2

EXIT_SEMANTICS = (
    "exit 0: task completed (PASS) | "
    "exit 1: task failed, blocked, cancelled, or interrupted | "
    "exit 2: usage error or missing session"
)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=None, help="working repository path")
    parser.add_argument("--verbose", action="store_true", help="include event detail")
    parser.add_argument("--json", action="store_true", help="machine-readable output, no ANSI codes")
    parser.add_argument("--model", default=None, help="model name or 'auto'")
    parser.add_argument("--provider", default=None, help="provider style or 'auto'")
    parser.add_argument("--approval-mode", default="standard", choices=APPROVAL_MODES)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yodaw",
        description="Native interactive YODAW agent shell",
        epilog=EXIT_SEMANTICS,
    )
    _add_common(parser)
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="run one task non-interactively")
    run_p.add_argument("goal", help="natural-language task")
    run_p.add_argument("--timeout", type=int, default=600)
    _add_common(run_p)

    status_p = sub.add_parser("status", help="show session/repo state without entering the REPL")
    _add_common(status_p)
    resume_p = sub.add_parser("resume", help="resume the latest or a named session")
    resume_p.add_argument("session_id", nargs="?")
    _add_common(resume_p)
    sessions_p = sub.add_parser("sessions", help="list persisted sessions")
    _add_common(sessions_p)
    config_p = sub.add_parser("config", help="manage product configuration")
    config_sub = config_p.add_subparsers(dest="config_command")
    init_p = config_sub.add_parser("init", help="write a default config file")
    init_p.add_argument("--path", default=None, help="write config to this path")
    init_p.add_argument("--force", action="store_true", help="overwrite an existing config")
    config_sub.add_parser("validate", help="validate the effective configuration")
    show_p = config_sub.add_parser("show", help="show the effective non-secret configuration")
    show_p.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("version", help="print the CLI version")
    return parser


def _result_exit(result: PipelineResult) -> int:
    return EXIT_OK if result.success else EXIT_TASK_FAILURE


def cmd_run(args: argparse.Namespace) -> int:
    """Non-interactive automation mode with meaningful exit codes."""
    from app.cli.repl import Repl
    repo = detect_repo(args.repo or ".")
    session = Session(
        repo=str(repo.root) if repo.root else None,
        branch=repo.branch,
        approval_mode=args.approval_mode,
        model=None if args.model in (None, "auto") else args.model,
        provider=None if args.provider in (None, "auto") else args.provider,
    )
    approved = args.approval_mode == "auto"
    result = run_task(
        args.goal,
        repo,
        session,
        approval_mode=args.approval_mode,
        approved=approved,
        confirm=None,
    )
    session.history.append({"role": "user", "text": args.goal})
    session.history.append({"role": "assistant", "text": f"[{result.status}] {result.summary}"[:2000]})
    session.evidence.extend(result.evidence[-25:])
    save_session(session)
    if args.json:
        payload = redact_mapping(
            {
                "status": result.status,
                "success": result.success,
                "summary": result.summary,
                "session_id": session.session_id,
                "events": result.events,
                "changed_files": result.changed_files,
                "error": result.error,
            }
        )
        print(json.dumps(payload, indent=2, default=str))
    else:
        helper = Repl(repo, session, verbose=args.verbose)
        helper.emit_events(result.events)
        if result.summary:
            helper.emit(result.summary)
        helper.emit(f"session: {session.session_id}")
    return _result_exit(result)


def cmd_status(args: argparse.Namespace) -> int:
    from app.cli.commands import cmd_status as render_status
    repo = detect_repo(args.repo or ".")
    target = latest_session_id()
    session = load_session(target) if target else Session(repo=str(repo.root) if repo.root else None, branch=repo.branch)
    if args.json:
        print(json.dumps(redact_mapping({"session": session.to_dict(), "repo": {"root": str(repo.root), "branch": repo.branch, "dirty": repo.dirty}}), indent=2, default=str))
    else:
        print(render_status(session, repo))
    return EXIT_OK


def cmd_resume(args: argparse.Namespace, extra: dict | None = None) -> int:
    from app.cli.repl import start_interactive
    session_id = getattr(args, "session_id", None) or (extra or {}).get("session_id")
    if session_id and load_session(session_id) is None:
        print(f"cannot resume session {session_id}", file=sys.stderr)
        return EXIT_USAGE
    return start_interactive(
        repo_path=args.repo,
        resume_id=session_id or latest_session_id(),
        approval_mode=args.approval_mode,
        verbose=args.verbose,
        json_mode=args.json,
        model=None if args.model == "auto" else args.model,
        provider=None if args.provider == "auto" else args.provider,
    )


def cmd_sessions(args: argparse.Namespace) -> int:
    items = list_sessions()
    if args.json:
        print(json.dumps(redact_mapping({"sessions": items}), indent=2, default=str))
    elif not items:
        print("no saved sessions")
    else:
        for item in items:
            print(f"{item['session_id']} repo={item.get('repo')} tasks={item.get('tasks')} updated={item.get('updated_at')}")
    return EXIT_OK

def cmd_config(args: argparse.Namespace) -> int:
    from app.product_config import (
        ConfigError,
        find_config_file,
        load_product_config,
        validate_product_config,
        write_default_config,
    )

    command = getattr(args, "config_command", None) or "show"

    if command == "init":
        try:
            written = write_default_config(
                getattr(args, "path", None),
                force=getattr(args, "force", False),
            )
        except ConfigError as exc:  # already exists, no --force
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        print(f"wrote {written}")
        return EXIT_OK

    if command == "validate":
        errors = validate_product_config()
        source = find_config_file()
        if errors:
            for error in errors:
                print(f"config error: {error}", file=sys.stderr)
            return EXIT_TASK_FAILURE
        print(
            f"configuration OK"
            f" ({source})" if source else "configuration OK (environment + defaults)"
        )
        return EXIT_OK

    if command == "show":
        try:
            cfg = load_product_config()
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return EXIT_TASK_FAILURE
        if args.json:
            print(json.dumps(cfg.to_redacted_dict(), indent=2))
        else:
            for line in cfg.summary_lines():
                print(line)
        return EXIT_OK

    print(f"unknown config command: {command}", file=sys.stderr)
    return EXIT_USAGE

def _apply_product_config() -> int:
    """Feed the config file into the environment before task commands. Env wins."""
    from app.product_config import ConfigError, apply_product_config

    try:
        apply_product_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK

TASK_COMMANDS = (None, "run", "status", "resume", "sessions")

def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv and dispatch; bare `yodaw` opens the interactive REPL."""
    parser = _build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and not raw[0].startswith("-") and raw[0] not in ("run", "status", "resume", "sessions", "config", "version", "-h", "--help"):
        # Convenience: `yodaw "fix tests"` behaves like `yodaw run`.
        raw = ["run", *raw]
    full = parser.parse_args(raw)

    # Task commands (REPL, run, status, resume, sessions) honor the
    # config file; `config` and `version` must work even when the
    # file is invalid, so they skip the apply step.
    if full.command in TASK_COMMANDS:
        code = _apply_product_config()
        if code != EXIT_OK:
            return code

    if full.command == "run":
        return cmd_run(full)
    if full.command == "status":
        return cmd_status(full)
    if full.command == "resume":
        return cmd_resume(full)
    if full.command == "sessions":
        return cmd_sessions(full)
    if full.command == "config":
        return cmd_config(full)
    if full.command == "version":
        print(f"yodaw {cli_version}")
        return EXIT_OK

    from app.cli.repl import start_interactive
    # A piped script still gets a working shell: the REPL reads
    # stdin line by line and exits cleanly on EOF. Color is
    # already off without a TTY, so output stays clean.
    return start_interactive(
        repo_path=full.repo,
        approval_mode=full.approval_mode,
        verbose=full.verbose,
        json_mode=full.json,
        model=None if full.model == "auto" else full.model,
        provider=None if full.provider == "auto" else full.provider,
    )


if __name__ == "__main__":
    sys.exit(main())
