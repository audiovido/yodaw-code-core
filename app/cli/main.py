"""Command entrypoint: `yodaw` interactive shell plus automation verbs."""

from __future__ import annotations

import argparse
import json
import os
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
    _add_llm_selection(init_p)
    set_p = config_sub.add_parser(
        "set", help="persist [llm] selection (creates the file if missing)"
    )
    set_p.add_argument("--path", default=None, help="config file to update")
    _add_llm_selection(set_p)
    config_sub.add_parser("validate", help="validate the effective configuration")
    show_p = config_sub.add_parser("show", help="show the effective non-secret configuration")
    show_p.add_argument("--json", action="store_true", help="machine-readable output")
    setup_p = sub.add_parser(
        "setup-9router",
        help="zero-touch 9Router provisioning (probe, detect model, persist config)",
    )
    setup_p.add_argument("--path", default=None, help="config file to write")
    setup_p.add_argument("--base-url", default=None, help="9Router base URL")
    setup_p.add_argument(
        "--model",
        default=None,
        help="pin a model/combo (default: auto-detect; 'auto' keeps runtime detection)",
    )
    setup_p.add_argument(
        "--api-key-env",
        default=None,
        help="env var holding the dashboard key (default: NINEROUTER_API_KEY)",
    )
    setup_p.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the verification chat after provisioning",
    )
    setup_p.add_argument(
        "--data-dir",
        default=None,
        help="9Router data directory (default: $DATA_DIR or ~/.9router)",
    )
    setup_p.add_argument(
        "--key-name",
        default=None,
        help="name of the local gateway key to provision",
    )
    setup_p.add_argument(
        "--install",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="install/start the 9Router daemon automatically (default)",
    )
    setup_p.add_argument(
        "--register-local",
        action="append",
        default=[],
        metavar="NAME:PREFIX:BASE_URL",
        help=(
            "register a local OpenAI-compatible server as a provider "
            "node + connection (repeatable), e.g. "
            "'Local llama (Gemma):gemma:http://127.0.0.1:8090/v1'"
        ),
    )
    setup_p.add_argument("--timeout", type=float, default=30.0)
    setup_p.add_argument("--json", action="store_true", help="machine-readable output")
    models_p = sub.add_parser(
        "models", help="list 9Router models/combos for the current configuration"
    )
    models_p.add_argument("--base-url", default=None, help="9Router base URL override")
    models_p.add_argument("--json", action="store_true", help="machine-readable output")
    kodgar_p = sub.add_parser(
        "kodgar",
        help=(
            "one-stop 9Router lifecycle owner: probe, ensure daemon, "
            "certify routes, persist config + certified fallback chain"
        ),
    )
    kodgar_p.add_argument("--path", default=None, help="config file to write")
    kodgar_p.add_argument("--base-url", default=None, help="9Router base URL")
    kodgar_p.add_argument(
        "--model",
        default=None,
        help="pin a model/combo (default: certify and pick the healthiest)",
   )
    kodgar_p.add_argument(
        "--api-key-env",
        default=None,
        help="env var holding the gateway key (default: NINEROUTER_API_KEY)",
    )
    kodgar_p.add_argument(
        "--data-dir",
        default=None,
        help="9Router data directory (default: $DATA_DIR or ~/.9router)",
    )
    kodgar_p.add_argument(
        "--key-name",
        default=None,
        help="name of the local gateway key to provision",
    )
    kodgar_p.add_argument(
        "--install",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="install/start the 9Router daemon automatically (default)",
    )
    kodgar_p.add_argument("--timeout", type=float, default=30.0)
    kodgar_p.add_argument("--json", action="store_true", help="machine-readable output")
    doctor_p = sub.add_parser(
        "kodgar-doctor",
        help="non-destructive health check: daemon, key, certified routes",
    )
    doctor_p.add_argument("--json", action="store_true", help="machine-readable output")
    sub.add_parser("version", help="print the CLI version")
    return parser


def _add_llm_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", default=None, help="llm provider")
    parser.add_argument("--mode", default=None, help="llm mode (local|remote|auto)")
    parser.add_argument("--model", default=None, help="model id or 'auto'")
    parser.add_argument("--base-url", default=None, help="provider endpoint")
    parser.add_argument(
        "--api-key-env", default=None, help="env var naming the API key"
    )


def _llm_selection(args: argparse.Namespace) -> dict:
    return {
        "provider": getattr(args, "provider", None),
        "mode": getattr(args, "mode", None),
        "model": getattr(args, "model", None),
        "base_url": getattr(args, "base_url", None),
        "api_key_env": getattr(args, "api_key_env", None),
    }


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
    # `run` executes real tasks: default to the worker-registry
    # executor so the non-interactive path can never degrade into
    # plan-only NOT_EXECUTED by a forgotten wiring.
    from app.cli.pipeline import default_executor

    result = run_task(
        args.goal,
        repo,
        session,
        approval_mode=args.approval_mode,
        approved=approved,
        confirm=None,
        executor=default_executor(),
        timeout=args.timeout,
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
    import os

    from app.product_config import (
        ConfigError,
        find_config_file,
        load_product_config,
        validate_product_config,
        write_default_config,
        write_llm_config,
    )

    command = getattr(args, "config_command", None) or "show"

    if command == "init":
        selection = {
            key: value
            for key, value in _llm_selection(args).items()
            if value is not None
        }
        target = getattr(args, "path", None)
        if selection and target and os.path.isfile(target):
            # `init --provider ...` on an existing file upserts the
            # selection instead of failing: provisioning stays
            # idempotent without --force.
            try:
                written = write_llm_config(target, **selection)
            except ConfigError as exc:
                print(f"config error: {exc}", file=sys.stderr)
                return EXIT_USAGE
            print(f"updated {written}")
            return EXIT_OK
        try:
            written = write_default_config(
                target,
                force=getattr(args, "force", False),
            )
        except ConfigError as exc:  # already exists, no --force
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        if selection:
            try:
                written = write_llm_config(written, **selection)
            except ConfigError as exc:
                print(f"config error: {exc}", file=sys.stderr)
                return EXIT_USAGE
        print(f"wrote {written}")
        return EXIT_OK

    if command == "set":
        selection = {
            key: value
            for key, value in _llm_selection(args).items()
            if value is not None
        }
        if not selection:
            print(
                "nothing to set; pass at least one of --provider "
                "--mode --model --base-url --api-key-env",
                file=sys.stderr,
            )
            return EXIT_USAGE
        try:
            written = write_llm_config(
                getattr(args, "path", None), **selection
            )
        except ConfigError as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(f"updated {written}")
        for key, value in selection.items():
            print(f"  {key} = {value}")
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

def cmd_setup_9router(args: argparse.Namespace) -> int:
    """Zero-touch 9Router provisioning.

    Full lifecycle, no manual copy/paste:

    1. ensure the official 9Router daemon is installed and running
       against a private data dir (install/start it when needed);
    2. derive the local admin token from the shared data dir and
       auto-provision the LOCAL gateway API key, persisting it in a
       0600 key file (upstream credentials are never touched);
    3. idempotently register local OpenAI-compatible model servers
       given via --register-local / YODAW_LOCAL_SERVERS;
    4. discover models/combos, pick a default, persist [llm] config;
    5. verify with one tiny chat unless --no-verify.
    """
    import os
    from pathlib import Path

    from app.llm import ninerouter
    from app.product_config import (
        ConfigError,
        config_file_candidates,
        write_api_key_file,
        write_llm_config,
    )

    base_url = (
        getattr(args, "base_url", None)
        or os.environ.get("YODAW_LLM_BASE_URL")
        or ninerouter.DEFAULT_BASE_URL
    )
    api_key_env = (
        getattr(args, "api_key_env", None) or ninerouter.DEFAULT_API_KEY_ENV
    )
    existing_key = (
        os.environ.get("YODAW_LLM_API_KEY", "")
        or os.environ.get(api_key_env, "")
        or ""
    )
    timeout = getattr(args, "timeout", 30.0) or 30.0
    as_json = bool(getattr(args, "json", False))
    may_install = bool(getattr(args, "install", True))

    data_dir_arg = (
        getattr(args, "data_dir", None)
        or os.environ.get("DATA_DIR")
        or None
    )
    data_dir = Path(data_dir_arg) if data_dir_arg else ninerouter.default_data_dir()
    key_name = getattr(args, "key_name", None) or ninerouter.DEFAULT_KEY_NAME

    # Local model servers: repeatable --register-local plus a
    # comma-separated env var (bootstrap zero-touch path).
    specs = list(getattr(args, "register_local", []) or [])
    specs.extend(
        part.strip()
        for part in os.environ.get("YODAW_LOCAL_SERVERS", "").split(",")
        if part.strip()
    )
    try:
        local_servers = [
            ninerouter.parse_local_server_spec(spec) for spec in specs
        ]
    except ninerouter.NinerouterAdminError as exc:
        print(f"setup-9router: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # ---- 1+2+3: daemon, admin token, gateway key, local nodes ----
    provisioned = None
    initial = ninerouter.detect_install(
        base_url, existing_key, timeout=min(timeout, 10.0)
    )
    need_admin = (
        not initial["reachable"]
        or not existing_key
        or bool(local_servers)
    )
    if need_admin:
        log_dir = Path.home() / ".yodaw" / "logs"
        try:
            from urllib.parse import urlparse

            parsed_port = urlparse(base_url).port
            provisioned = ninerouter.auto_provision(
                base_url,
                data_dir=data_dir,
                port=parsed_port or ninerouter.DEFAULT_PORT,
                key_name=key_name,
                existing_key=existing_key,
                local_servers=local_servers,
                install=may_install,
                start=True,
                log_file=log_dir / "9router.log",
            )
            api_key = (
                provisioned["gateway_key"].get("key") or existing_key
            )
        except ninerouter.NinerouterAdminError as exc:
            # Already reachable with a valid env key and no admin
            # data dir? Keep legacy manual mode working.
            if initial["reachable"] and existing_key and not local_servers:
                print(
                    f"warning: automatic key provisioning unavailable "
                    f"({exc}); continuing with {api_key_env} from the "
                    "environment",
                    file=sys.stderr,
                )
                api_key = existing_key
                provisioned = None
            else:
                if as_json:
                    print(
                        json.dumps(
                            {"ok": False, "error": str(exc)}, indent=2
                        )
                    )
                else:
                    print(f"setup-9router: {exc}", file=sys.stderr)
                return EXIT_TASK_FAILURE
    else:
        api_key = existing_key

    # ---- 4: discovery ------------------------------------------------
    detection = ninerouter.detect_install(base_url, api_key, timeout=timeout)
    normalized = detection["base_url"]
    probe = detection["probe"]

    if not detection["reachable"]:
        message = probe.get("error", "9Router became unreachable")
        if as_json:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(f"setup-9router: {message}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    models = probe.get("models", [])
    combos = probe.get("combos", [])
    detected = probe.get("default_model")
    requested = getattr(args, "model", None)

    if detected is None and not (requested and requested.strip() != "auto"):
        message = probe.get(
            "warning",
            "9Router reports no models and no combos.",
        )
        if as_json:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(f"setup-9router: {message}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    # ---- health-aware route certification --------------------------
    # A listed combo/model is not necessarily usable (upstream free
    # tiers may 403). Certify candidates with REAL tiny chats and
    # select the primary plus fallbacks ONLY from routes that passed.
    healthy: list[dict] = []
    rejected: list[dict] = []
    certified: list[str] = []
    if not (requested and requested.strip() != "auto"):
        try:
            chain = ninerouter.build_fallback_chain(
                models,
                combos,
                base_url=normalized,
                api_key=api_key,
                wanted=3,
                timeout=max(timeout, 30.0),
            )
        except ninerouter.NinerouterError as exc:
            if as_json:
                print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
            else:
                print(f"setup-9router: {exc}", file=sys.stderr)
            return EXIT_TASK_FAILURE
        healthy = chain["healthy"]
        rejected = chain["rejected"]
        certified = [r["model"] for r in healthy]
        if healthy:
            model = healthy[0]["model"]
        else:
            summary = "; ".join(
                f"{r['model']}={r['classification']}" for r in rejected[:6]
            )
            message = (
                "no 9Router route completed a real chat request "
                f"({summary}). Fix the upstream provider in the "
                "dashboard (e.g. free-tier credentials) or pin a "
                "known-good model with --model."
            )
            if as_json:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": message,
                            "rejected": [
                                {
                                    "model": r["model"],
                                    "classification": r["classification"],
                                }
                                for r in rejected
                            ],
                        },
                        indent=2,
                    )
                )
            else:
                print(f"setup-9router: {message}", file=sys.stderr)
            return EXIT_TASK_FAILURE
        for result in rejected:
            print(
                f"route rejected: {result['model']} -> "
                f"{result['classification']}",
                file=sys.stderr,
            )
    else:
        model = requested.strip()
    if model and model != "auto":
        known = set(models) | set(combos)
        if model not in known:
            print(
                f"warning: {model!r} is not in the current "
                f"9Router inventory ({len(models)} models, "
                f"{len(combos)} combos); persisting anyway",
                file=sys.stderr,
            )

    # ---- persist: 0600 key file + TOML (never the raw key) ----------
    config_target = getattr(args, "path", None) or config_file_candidates()[1]
    key_file_path = (
        Path(os.path.dirname(os.path.abspath(config_target)))
        / "secrets"
        / "9router-api.key"
    )
    try:
        if api_key:
            key_file = write_api_key_file(api_key, str(key_file_path))
        else:
            key_file = None
        written = write_llm_config(
            config_target,
            provider="9router",
            model=model,
            base_url=normalized,
            api_key_env=api_key_env,
            api_key_file=key_file,
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"could not persist gateway key: {exc}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    # Persist the certified healthy chain into the environment the
    # runtime already reads (YODAW_LLM_FALLBACK_MODELS), for BOTH the
    # current process and future shells (launchd plist + shell rc).
    # Existing environment values win, so manual operator overrides
    # are never clobbered.
    fallback_route = ",".join(certified[1:]) if len(certified) > 1 else ""
    if fallback_route and "YODAW_LLM_FALLBACK_MODELS" not in os.environ:
        os.environ["YODAW_LLM_FALLBACK_MODELS"] = fallback_route
    exported_fallbacks = False
    if fallback_route:
        for rc in (
            Path(os.path.expanduser("~")) / ".zshrc",
            Path(os.path.expanduser("~")) / ".bashrc",
        ):
            marker = "# kodgar: certified 9Router fallback routes"
            try:
                rc_text = rc.read_text(encoding="utf-8") if rc.exists() else ""
            except OSError:
                rc_text = ""
            if marker in rc_text:
                # Keep the certified chain current (a previously
                # exported route may have since failed certification).
                new_block = (
                    f"{marker}\n"
                    f'export YODAW_LLM_FALLBACK_MODELS="{fallback_route}"\n'
                )
                import re as _re

                updated = _re.sub(
                    marker + r"\nexport YODAW_LLM_FALLBACK_MODELS=\"[^\"\n]*\"\n",
                    new_block,
                    rc_text,
                )
                if updated != rc_text:
                    try:
                        rc.write_text(updated, encoding="utf-8")
                        exported_fallbacks = True
                    except OSError:
                        pass
            else:
                block = (
                    f"\n{marker}\n"
                    f'export YODAW_LLM_FALLBACK_MODELS="{fallback_route}"\n'
                )
                try:
                    with open(rc, "a", encoding="utf-8") as handle:
                        handle.write(block)
                    exported_fallbacks = True
                except OSError:
                    pass

    # ---- 5: verification ---------------------------------------------
    verified: object = "skipped"
    if not getattr(args, "no_verify", False):
        from app.llm.provider import LLMError, LocalLLMProvider

        try:
            provider = LocalLLMProvider(
                style="9router",
                base_url=normalized,
                model=model,
                api_key=api_key,
            )
            provider.chat(
                "You are a provisioning check. Reply exactly.",
                "Reply with exactly: OK",
            )
            verified = True
        except LLMError as exc:
            if as_json:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": str(exc),
                            "config": written,
                            "key_file": key_file,
                            "model": model,
                        },
                        indent=2,
                    )
                )
            else:
                print(f"verification chat failed: {exc}", file=sys.stderr)
                print(
                    f"(selection was still persisted to {written}; "
                    "connect a capable route and rerun, or pass "
                    "--no-verify)",
                    file=sys.stderr,
                )
            return EXIT_TASK_FAILURE

    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "config": written,
                    "key_file": key_file,
                    "provider": "9router",
                    "model": model,
                    "detected_default": detected,
                    "base_url": normalized,
                    "api_key_env": api_key_env,
                    "provisioning": _provisioning_summary(provisioned),
                    "models": len(models),
                    "combos": len(combos),
                    "verified": verified,
                    "healthy_routes": certified,
                    "rejected_routes": [
                        {
                            "model": r["model"],
                            "classification": r["classification"],
                        }
                        for r in rejected
                    ],
                    "fallback_models_exported": exported_fallbacks,
                },
                indent=2,
            )
        )
        return EXIT_OK

    print("9Router is ready:")
    print(f"  endpoint : {normalized}")
    if provisioned:
        daemon = provisioned.get("daemon", {})
        if daemon.get("started"):
            print(f"  daemon   : started (pid {daemon.get('pid')})")
        gk = provisioned.get("gateway_key", {})
        print(f"  gateway  : key {gk.get('status')} ({gk.get('name')})")
        for server in provisioned.get("local_servers", []):
            print(
                f"  local    : {server['prefix']} -> {server['base_url']} "
                f"(node {'created' if server['node_created'] else 'present'}, "
                f"connection {'created' if server['connection_created'] else 'present'})"
            )
    print(f"  inventory: {len(models)} models, {len(combos)} combos")
    if combos:
        print(f"  combos   : {', '.join(combos[:8])}")
    print(f"  model    : {model}")
    if certified:
        print(f"  healthy  : {', '.join(certified)}")
        for result in rejected:
            print(
                f"  rejected : {result['model']} "
                f"({result['classification']})"
            )
    print(f"  config   : {written}")
    print(f"  key file : {key_file or 'n/a (using environment)'}")
    print(f"  verified : {verified}")
    return EXIT_OK


def _provisioning_summary(provisioned) -> object:
    """JSON-safe provisioning summary with every secret stripped."""
    if not provisioned:
        return None
    gateway_key = dict(provisioned.get("gateway_key") or {})
    gateway_key.pop("key", None)  # never emit the raw gateway key
    return {
        "base_url": provisioned.get("base_url"),
        "data_dir": provisioned.get("data_dir"),
        "daemon": provisioned.get("daemon"),
        "gateway_key": gateway_key,
        "local_servers": provisioned.get("local_servers"),
    }


def cmd_kodgar(args: argparse.Namespace) -> int:
    """Single owner of the 9Router lifecycle (probe->certify->persist).

    Never starts a second daemon when one already answers; reuses the
    existing key when valid; certifies routes with REAL tiny chats;
    persists the healthy chain (primary + fallbacks) so the runtime
    can fail over without re-certifying.
    """
    from pathlib import Path
    from urllib.parse import urlparse

    from app.llm import ninerouter
    from app.product_config import (
        ConfigError,
        config_file_candidates,
        write_api_key_file,
        write_llm_config,
    )

    as_json = bool(getattr(args, "json", False))
    base_url = (
        getattr(args, "base_url", None)
        or ninerouter.DEFAULT_BASE_URL
    )
    api_key_env = (
        getattr(args, "api_key_env", None) or ninerouter.DEFAULT_API_KEY_ENV
    )
    existing_key = os.environ.get(api_key_env, "")
    requested = (getattr(args, "model", None) or "").strip()
    may_install = bool(getattr(args, "install", True))
    timeout = float(getattr(args, "timeout", 30.0) or 30.0)
    data_dir = getattr(args, "data_dir", None)
    data_dir = Path(data_dir) if data_dir else None
    key_name = (
        getattr(args, "key_name", None) or ninerouter.DEFAULT_KEY_NAME
    )

    # ---- ensure one reachable daemon --------------------------------
    provisioned = None
    if not ninerouter.daemon_health(base_url):
        if not may_install:
            message = (
                f"9Router daemon not reachable at {base_url} and "
                "--no-install given"
            )
            if as_json:
                print(json.dumps({"ok": False, "error": message}, indent=2))
            else:
                print(f"kodgar: {message}", file=sys.stderr)
            return EXIT_TASK_FAILURE
        log_dir = Path.home() / ".yodaw" / "logs"
        try:
            provisioned = ninerouter.auto_provision(
                base_url,
                data_dir=data_dir,
                port=urlparse(base_url).port or ninerouter.DEFAULT_PORT,
                key_name=key_name,
                existing_key=existing_key,
                install=True,
                start=True,
                log_file=log_dir / "9router.log",
            )
            api_key = (
                provisioned["gateway_key"].get("key") or existing_key
            )
        except ninerouter.NinerouterAdminError as exc:
            message = str(exc)
            if as_json:
                print(json.dumps({"ok": False, "error": message}, indent=2))
            else:
                print(f"kodgar: {message}", file=sys.stderr)
            return EXIT_TASK_FAILURE
    else:
        api_key = existing_key

    if not ninerouter.daemon_health(base_url):
        message = f"9Router daemon still not reachable at {base_url}"
        if as_json:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(f"kodgar: {message}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    # ---- inventory + health-aware certification ----------------------
    try:
        listing = ninerouter.list_models(base_url, api_key)
    except ninerouter.NinerouterError as exc:
        message = str(exc)
        if as_json:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(f"kodgar: {message}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    models = listing["models"]
    combos = listing["combos"]
    if requested and requested != "auto":
        model = requested
        certified = [requested]
        rejected = []
        if requested not in (set(models) | set(combos)):
            print(
                f"warning: {requested!r} is not in the current 9Router "
                f"inventory; persisting anyway",
                file=sys.stderr,
            )
    else:
        try:
            chain = ninerouter.build_fallback_chain(
                models,
                combos,
                base_url=base_url,
                api_key=api_key,
                wanted=3,
                timeout=max(timeout, 30.0),
            )
        except ninerouter.NinerouterError as exc:
            message = str(exc)
            if as_json:
                print(json.dumps({"ok": False, "error": message}, indent=2))
            else:
                print(f"kodgar: {message}", file=sys.stderr)
            return EXIT_TASK_FAILURE
        healthy = chain["healthy"]
        rejected = chain["rejected"]
        certified = [r["model"] for r in healthy]
        if not certified:
            summary = "; ".join(
                f"{r['model']}={r['classification']}" for r in rejected[:6]
            )
            message = (
                "no 9Router route completed a real chat request "
                f"({summary}). Connect a working provider in the "
                "dashboard or pin one with --model."
            )
            if as_json:
                print(json.dumps({"ok": False, "error": message}, indent=2))
            else:
                print(f"kodgar: {message}", file=sys.stderr)
            return EXIT_TASK_FAILURE
        model = certified[0]
        for result in rejected:
            print(
                f"route rejected: {result['model']} -> "
                f"{result['classification']}",
                file=sys.stderr,
            )

    # ---- persist config + certified fallback chain -------------------
    config_target = getattr(args, "path", None) or config_file_candidates()[1]
    key_file_path = (
        Path(os.path.dirname(os.path.abspath(config_target)))
        / "secrets"
        / "9router-api.key"
    )
    try:
        key_file = (
            write_api_key_file(api_key, str(key_file_path)) if api_key else None
        )
        written = write_llm_config(
            config_target,
            provider="9router",
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_key_file=key_file,
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except OSError as exc:
        print(f"could not persist gateway key: {exc}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    fallback_route = ",".join(certified[1:]) if len(certified) > 1 else ""
    if fallback_route and "YODAW_LLM_FALLBACK_MODELS" not in os.environ:
        os.environ["YODAW_LLM_FALLBACK_MODELS"] = fallback_route

    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "config": written,
                    "key_file": key_file,
                    "model": model,
                    "base_url": base_url,
                    "healthy_routes": certified,
                    "rejected_routes": [
                        {
                            "model": r["model"],
                            "classification": r["classification"],
                        }
                        for r in rejected
                    ],
                },
                indent=2,
            )
        )
        return EXIT_OK

    print("kodgar: 9Router lifecycle complete")
    print(f"  endpoint : {base_url}")
    print(f"  model    : {model}")
    if certified:
        print(f"  healthy  : {', '.join(certified)}")
        for result in rejected:
            print(
                f"  rejected : {result['model']} "
                f"({result['classification']})"
            )
    print(f"  config   : {written}")
    print(f"  key file : {key_file or 'n/a (using environment)'}")
    return EXIT_OK


def cmd_kodgar_doctor(args: argparse.Namespace) -> int:
    """Non-destructive health report; exit 1 when the gateway is down."""
    from app.llm import ninerouter
    from app.product_config import ConfigError, load_product_config

    as_json = bool(getattr(args, "json", False))
    try:
        cfg = load_product_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    base_url = cfg.base_url
    api_key = cfg.api_key
    checks: list[dict] = []

    cli_path = ninerouter.find_cli()
    checks.append(
        {
            "name": "cli",
            "ok": bool(cli_path),
            "detail": cli_path or "9router CLI not found on PATH",
        }
    )

    # ---- execution-path checks (safe, non-mutating) ----------------
    # The doctor must not report healthy while the CLI itself cannot
    # execute a task: the wiring defect that shipped once as plan-only
    # NOT_EXECUTED output must be caught here.
    from app.cli.pipeline import default_executor
    from app.workers.registry import registry as worker_registry

    try:
        executor = default_executor()
        executor_wiring_ok = callable(executor)
        executor_detail = "default executor wired" if executor_wiring_ok else "no default executor"
    except Exception as exc:
        executor = None
        executor_wiring_ok = False
        executor_detail = f"executor wiring error: {exc}"
    checks.append(
        {"name": "executor wiring", "ok": executor_wiring_ok, "detail": executor_detail}
    )

    try:
        workers = worker_registry.status()
        ready = [w for w in workers if w.get("status") == "READY"]
        caps = sorted({c for w in workers for c in (w.get("capabilities") or [])})
        workers_ok = bool(ready) and bool(caps)
        workers_detail = (
            f"{len(ready)}/{len(workers)} ready; capabilities: {', '.join(caps)}"
            if workers_ok
            else "no worker reports READY"
        )
    except Exception as exc:
        workers_ok = False
        workers_detail = f"worker registry error: {exc}"
    checks.append({"name": "worker registry", "ok": workers_ok, "detail": workers_detail})

    llm_inference_ok = False
    llm_detail = ""
    try:
        from app.llm.provider import LocalLLMProvider, LLMError

        provider = LocalLLMProvider()
        reply = provider.chat(
            "You are a health check. Reply with exactly: OK",
            "Reply with exactly: OK",
        )
        llm_inference_ok = "OK" in (reply or "")
        llm_detail = (
            f"style={provider.style} model={provider.model} reply={reply[:60]!r}"
            if llm_inference_ok
            else f"unexpected reply {reply[:60]!r}"
        )
    except Exception as exc:
        llm_detail = f"inference failed: {type(exc).__name__}: {exc}"[:200]
    checks.append({"name": "llm inference", "ok": llm_inference_ok, "detail": llm_detail})

    reachable = ninerouter.daemon_health(base_url)
    checks.append(
        {
            "name": "daemon",
            "ok": reachable,
            "detail": base_url if reachable else "not reachable",
        }
    )

    inventory = None
    if reachable:
        try:
            listing = ninerouter.list_models(base_url, api_key)
            inventory = {
                "models": len(listing["models"]),
                "combos": len(listing["combos"]),
            }
            checks.append(
                {
                    "name": "inventory",
                    "ok": bool(listing["models"] or listing["combos"]),
                    "detail": (
                        f"{len(listing['models'])} models, "
                        f"{len(listing['combos'])} combos"
                    ),
                }
            )
        except ninerouter.NinerouterError as exc:
            checks.append({"name": "inventory", "ok": False, "detail": str(exc)})
    else:
        checks.append(
            {"name": "inventory", "ok": False, "detail": "skipped (daemon down)"}
        )

    certification = None
    if reachable and (inventory is None or any(
        inventory.values()
    )):
        try:
            chain = ninerouter.build_fallback_chain(
                (listing["models"] if inventory else []),
                (listing["combos"] if inventory else []),
                base_url=base_url,
                api_key=api_key,
                wanted=1,
                timeout=30.0,
            )
        except ninerouter.NinerouterError as exc:
            chain = {"healthy": [], "rejected": []}
            checks.append({"name": "certified route", "ok": False, "detail": str(exc)})
        else:
            healthy = chain["healthy"]
            rejected = chain["rejected"]
            certification = {
                "primary": healthy[0]["model"] if healthy else None,
                "rejected": [
                    {
                        "model": r["model"],
                        "classification": r["classification"],
                    }
                    for r in rejected
                ],
            }
            if healthy:
                checks.append(
                    {
                        "name": "certified route",
                        "ok": True,
                        "detail": (
                            f"primary={healthy[0]['model']} "
                            f"({len(rejected)} rejected)"
                        ),
                    }
                )
            else:
                checks.append(
                    {
                        "name": "certified route",
                        "ok": False,
                        "detail": (
                            "no route completed a real chat "
                            + "; ".join(
                                f"{r['model']}={r['classification']}"
                                for r in rejected[:4]
                            )
                        ),
                    }
                )
    else:
        checks.append(
            {
                "name": "certified route",
                "ok": False,
                "detail": "skipped (no reachable inventory)",
            }
        )

    ok = all(check["ok"] for check in checks)
    if as_json:
        print(
            json.dumps(
                {"ok": ok, "base_url": base_url, "checks": checks},
                indent=2,
            )
        )
        return EXIT_OK if ok else EXIT_TASK_FAILURE

    print(f"kodgar-doctor: {'healthy' if ok else 'UNHEALTHY'} ({base_url})")
    for check in checks:
        mark = "ok" if check["ok"] else "FAIL"
        print(f"  [{mark:4}] {check['name']}: {check['detail']}")
    return EXIT_OK if ok else EXIT_TASK_FAILURE


def cmd_models(args: argparse.Namespace) -> int:
    """List the 9Router inventory for the current configuration."""
    from app.llm import ninerouter
    from app.product_config import ConfigError, load_product_config

    base_override = getattr(args, "base_url", None)
    as_json = bool(getattr(args, "json", False))

    try:
        cfg = load_product_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    base_url = base_override or cfg.base_url
    api_key = cfg.api_key

    if cfg.provider != "9router" and not base_override:
        print(
            f"model inventory needs the 9router provider "
            f"(current: {cfg.provider}); run `yodaw setup-9router` "
            f"or pass --base-url",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        listing = ninerouter.list_models(base_url, api_key)
        default = ninerouter.pick_default_model(
            listing["models"], listing["combos"]
        )
    except ninerouter.NinerouterError as exc:
        print(f"models: {exc}", file=sys.stderr)
        return EXIT_TASK_FAILURE

    if as_json:
        print(
            json.dumps(
                {
                    "base_url": listing["base_url"],
                    "default_model": default,
                    "combos": listing["combos"],
                    "models": listing["models"],
                },
                indent=2,
            )
        )
        return EXIT_OK

    print(f"9Router inventory at {listing['base_url']}:")
    print(f"  default: {default}")
    print(f"  combos ({len(listing['combos'])}):")
    for combo in listing["combos"]:
        print(f"    {combo}")
    print(f"  models ({len(listing['models'])}):")
    for model in listing["models"]:
        print(f"    {model}")
    return EXIT_OK


def _apply_product_config() -> int:
    """Feed the config file into the environment before task commands. Env wins."""
    from app.product_config import ConfigError, apply_product_config

    try:
        apply_product_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK

TASK_COMMANDS = (
    None,
    "run",
    "status",
    "resume",
    "sessions",
    "setup-9router",
    "kodgar",
    "kodgar-doctor",
    "models",
)

def main(argv: Sequence[str] | None = None) -> int:
    """Parse argv and dispatch; bare `yodaw` opens the interactive REPL."""
    parser = _build_parser()
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and not raw[0].startswith("-") and raw[0] not in (
        "run", "status", "resume", "sessions", "config",
        "setup-9router", "kodgar", "kodgar-doctor", "models",
        "version", "-h", "--help",
    ):
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
    if full.command == "setup-9router":
        return cmd_setup_9router(full)
    if full.command == "kodgar":
        return cmd_kodgar(full)
    if full.command == "kodgar-doctor":
        return cmd_kodgar_doctor(full)
    if full.command == "models":
        return cmd_models(full)
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
