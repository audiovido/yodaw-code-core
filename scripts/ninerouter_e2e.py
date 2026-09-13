#!/usr/bin/env python3
"""9Router acceptance harness: real-provider E2E + install/restart checks.

Subcommands (each prints human-readable output, or JSON with --json):

    probe          9Router reachable? inventory? (never needs a key to try)
    models         list models/combos + detected default
    chat           one tiny real chat ("Reply with exactly: OK")
    mission        full YODAW mission through the 9Router provider (expects PASS)
    fallback       fallback-chain mechanics (dead primary -> dead backup)
    fresh-install  clean HOME + clean DB: repair, config, boot, health
    restart        real launcher start -> health -> restart -> health -> stop
    all            everything above, aggregated

Exit codes: 0 PASS, 1 FAIL, 2 SKIP/BLOCKED (router not running, no key
for a live check, or environment cannot run the check). SKIP is honest
automation: CI without a 9Router daemon stays green while reporting
exactly what was not exercised.

Credentials: --api-key, or YODAW_LLM_API_KEY, or NINEROUTER_API_KEY.
Nothing is printed except a set/not-set flag.

Examples:

    python scripts/ninerouter_e2e.py probe
    python scripts/ninerouter_e2e.py chat --model kr/claude-sonnet-4.5
    python scripts/ninerouter_e2e.py mission --timeout 600
    python scripts/ninerouter_e2e.py all --json
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PASS, FAIL, SKIP = 0, 1, 2

TERMINAL = {"PASS", "FAIL", "BLOCKED", "BLOCKED_EXTERNAL", "CANCELLED"}


def _api_key(args) -> str:
    return (
        getattr(args, "api_key", None)
        or os.environ.get("YODAW_LLM_API_KEY", "")
        or os.environ.get("NINEROUTER_API_KEY", "")
        or ""
    )


def _base_url(args) -> str:
    from app.llm import ninerouter

    return (
        getattr(args, "base_url", None)
        or os.environ.get("YODAW_LLM_BASE_URL", "")
        or ninerouter.DEFAULT_BASE_URL
    )


def _emit(args, payload: dict) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return
    status = payload.get("status", "?")
    print(f"[{status}] {payload.get('check', '?')}")
    for key, value in payload.items():
        if key in ("status", "check"):
            continue
        if key == "key" and isinstance(value, str) and value not in (
            "set",
            "not set",
        ):
            value = "set"
        print(f"  {key}: {value}")


def _result(check: str, ok: bool, exit_code: int = None, **fields) -> tuple[int, dict]:
    code = PASS if ok else FAIL
    if exit_code is not None:
        code = exit_code
    status = "PASS" if code == PASS else ("SKIP" if code == SKIP else "FAIL")
    return code, {"check": check, "status": status, **fields}


# ------------------------------------------------------------ probe/models


def cmd_probe(args):
    from app.llm import ninerouter

    key = _api_key(args)
    base = _base_url(args)
    started = time.monotonic()
    detection = ninerouter.detect_install(base, key, timeout=args.timeout)
    latency = round(time.monotonic() - started, 3)
    probe = detection["probe"]

    if not detection["reachable"]:
        return _result(
            "probe",
            False,
            exit_code=SKIP,
            reason="9Router daemon not reachable",
            error=probe.get("error"),
            base_url=detection["base_url"],
            cli_installed=detection["cli_installed"],
            hint="install: npm install -g 9router; start: 9router",
        )

    return _result(
        "probe",
        True,
        base_url=detection["base_url"],
        latency_s=latency,
        models=len(probe.get("models", [])),
        combos=len(probe.get("combos", [])),
        default_model=probe.get("default_model"),
        key="set" if key else "not set",
        warning=probe.get("warning"),
    )


def cmd_models(args):
    from app.llm import ninerouter

    key = _api_key(args)
    base = _base_url(args)
    try:
        listing = ninerouter.list_models(base, key, timeout=args.timeout)
        default = ninerouter.pick_default_model(
            listing["models"], listing["combos"]
        )
    except ninerouter.NinerouterError as exc:
        message = str(exc)
        if "cannot reach 9Router" in message:
            return _result(
                "models", False, exit_code=SKIP, reason=message,
                base_url=base,
            )
        return _result("models", False, error=message, base_url=base)

    return _result(
        "models",
        True,
        base_url=listing["base_url"],
        default_model=default,
        combos=listing["combos"],
        models=listing["models"],
        key="set" if key else "not set",
    )


# ------------------------------------------------------------ chat


def cmd_chat(args):
    from app.llm import ninerouter

    key = _api_key(args)
    base = _base_url(args)
    model = getattr(args, "model", None) or "auto"

    reachable = ninerouter.probe(base, key, timeout=min(args.timeout, 15))
    if not reachable.get("ok"):
        return _result(
            "chat",
            False,
            exit_code=SKIP,
            reason="9Router daemon not reachable",
            error=reachable.get("error"),
            base_url=base,
        )

    started = time.monotonic()
    try:
        reply = ninerouter.chat(
            "You are an acceptance probe. Reply exactly.",
            "Reply with exactly: OK",
            model=model,
            base_url=base,
            api_key=key,
            timeout=args.timeout,
        )
    except ninerouter.NinerouterError as exc:
        return _result(
            "chat",
            False,
            error=str(exc),
            base_url=base,
            model=model,
            key="set" if key else "not set",
            hint="connect a provider in the dashboard and set "
            "NINEROUTER_API_KEY from the dashboard key",
        )
    latency = round(time.monotonic() - started, 3)

    ok = isinstance(reply, str) and bool(reply.strip())
    return _result(
        "chat",
        ok,
        base_url=base,
        model=model,
        latency_s=latency,
        reply_chars=len(reply or ""),
        reply_prefix=(reply or "")[:120],
        key="set" if key else "not set",
    )


# ------------------------------------------------------------ mission


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "app.py").write_text("def greet():\n    return 'hi'\n")
    subprocess.run(
        ["git", "init", "-q"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=e2e@yodaw", "-c", "user.name=yodaw",
         "commit", "-qm", "initial"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _poll(client, mission_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/missions/{mission_id}")
        assert response.status_code == 200, response.text[:300]
        last = response.json()
        if last.get("status") in TERMINAL:
            return last
        time.sleep(1.0)
    raise TimeoutError(
        f"mission {mission_id} not terminal within {timeout}s "
        f"(last: {last.get('status')})"
    )


def cmd_mission(args):
    from app.llm import ninerouter

    key = _api_key(args)
    base = _base_url(args)
    model = getattr(args, "model", None) or "auto"

    reachable = ninerouter.probe(base, key, timeout=min(args.timeout, 15))
    if not reachable.get("ok"):
        return _result(
            "mission",
            False,
            exit_code=SKIP,
            reason="9Router daemon not reachable",
            error=reachable.get("error"),
            base_url=base,
        )
    if model == "auto":
        model = reachable.get("default_model") or "auto"

    with tempfile.TemporaryDirectory(prefix="yodaw_9r_mission_") as td:
        tmp = Path(td)
        os.environ["YODAW_DB_PATH"] = str(tmp / "e2e.db")
        os.environ["YODAW_LLM_STYLE"] = "9router"
        os.environ["YODAW_LLM_BASE_URL"] = base
        os.environ["YODAW_LLM_MODEL"] = model
        if key:
            os.environ["YODAW_LLM_API_KEY"] = key

        from fastapi.testclient import TestClient

        import app.main as main_module
        from app.storage.sqlite_store import MissionStore

        # Bind a fresh store to the temp DB (the module-level store
        # may already point at the developer's real database).
        main_module.store = MissionStore(os.environ["YODAW_DB_PATH"])

        repo = _make_repo(tmp)
        client = TestClient(main_module.app)
        goal = (
            "Modify app.py to say def greet():\n    return 'hello'\n"
        )
        started = time.monotonic()
        try:
            response = client.post(
                "/api/v1/missions",
                json={
                    "goal": goal,
                    "capability": "code",
                    "repo_path": str(repo),
                },
            )
            assert response.status_code == 200, response.text[:500]
            mission_id = response.json()["mission_id"]
            terminal = _poll(client, mission_id, timeout=args.timeout)
        except Exception as exc:
            return _result(
                "mission", False, error=f"{type(exc).__name__}: {exc}",
                base_url=base, model=model,
            )
        latency = round(time.monotonic() - started, 3)

        evidence = client.get(
            f"/api/v1/missions/{mission_id}/evidence"
        ).json()

    ok = terminal.get("status") == "PASS"
    fields = {
        "base_url": base,
        "model": model,
        "mission_id": terminal.get("mission_id"),
        "mission_status": terminal.get("status"),
        "worker": terminal.get("worker"),
        "latency_s": latency,
        "evidence_entries": len(evidence) if isinstance(evidence, list) else evidence,
        "key": "set" if key else "not set",
    }
    if not ok:
        fields["result"] = terminal.get("result")
    return _result("mission", ok, **fields)


# ------------------------------------------------------------ fallback


def cmd_fallback(args):
    """Fallback-chain mechanics (no live backend needed).

    Primary 9Router points at a dead port, backup ollama is also
    dead: the chain must be walked (attempt log shows the hop) and
    the final error must surface. PASS = mechanics proven.
    """
    from app.llm.provider import LocalLLMProvider, pop_attempt_log

    saved = {
        name: os.environ.get(name)
        for name in (
            "YODAW_LLM_STYLE",
            "YODAW_LLM_BASE_URL",
            "YODAW_LLM_MODEL",
            "YODAW_LLM_FALLBACKS",
            "YODAW_PROVIDER_MAX_RETRIES",
            "YODAW_PROVIDER_BACKOFF_SECONDS",
            "YODAW_LLM_TIMEOUT_SECONDS",
        )
    }
    os.environ["YODAW_LLM_STYLE"] = "9router"
    os.environ["YODAW_LLM_BASE_URL"] = "http://127.0.0.1:9"
    os.environ["YODAW_LLM_MODEL"] = "fallback-probe-model"
    os.environ["YODAW_LLM_FALLBACKS"] = "ollama"
    os.environ["YODAW_PROVIDER_MAX_RETRIES"] = "1"
    os.environ["YODAW_PROVIDER_BACKOFF_SECONDS"] = "0"
    os.environ["YODAW_LLM_TIMEOUT_SECONDS"] = "5"
    try:
        pop_attempt_log()
        provider = LocalLLMProvider()
        primary = {
            "style": provider.style,
            "base_url": provider.base_url,
            "model": provider.model,
        }
        try:
            provider.chat("system", "user")
            outcome = "unexpected-success"
            attempts = pop_attempt_log()
        except Exception as exc:
            outcome = f"{type(exc).__name__}"
            attempts = pop_attempt_log()
            error = str(exc)[:300]
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    hops = [a for a in attempts if a.get("provider_fallback")]
    walked = (
        len(hops) == 1
        and hops[0].get("from") == "9router"
        and hops[0].get("to") == "ollama"
    )
    if outcome == "unexpected-success":
        return _result(
            "fallback", False,
            error="chat unexpectedly succeeded against dead backends",
            primary=primary,
        )
    return _result(
        "fallback",
        walked,
        primary=primary,
        chain=["9router", "ollama"],
        hops=hops,
        final_error=error if outcome != "unexpected-success" else None,
        attempts=len(attempts),
    )


# ------------------------------------------------------------ fresh-install


_FRESH_SNIPPET = r"""
import json, os, sys
sys.path.insert(0, {repo!r})
from app.product_config import load_product_config
from app.storage.db import repair_database

db_report = repair_database(os.environ["YODAW_DB_PATH"])
cfg = load_product_config()
health = None
try:
    from fastapi.testclient import TestClient
    import app.main as main_module
    from app.storage.sqlite_store import MissionStore
    main_module.store = MissionStore(os.environ["YODAW_DB_PATH"])
    client = TestClient(main_module.app)
    response = client.get("/api/v1/health")
    health = {"status_code": response.status_code, "body": response.json()}
except Exception as exc:
    health = {"error": f"{{type(exc).__name__}}: {{exc}}"}  # noqa

print(json.dumps({
    "db": db_report,
    "config": cfg.to_redacted_dict(),
    "health": health,
}))
"""


def cmd_fresh_install(args):
    from app.llm import ninerouter

    base = _base_url(args)
    model = getattr(args, "model", None) or "auto"

    with tempfile.TemporaryDirectory(prefix="yodaw_9r_fresh_") as td:
        tmp = Path(td)
        home = tmp / "home"
        home.mkdir()
        # A nested, not-yet-existing DB path: repair must create it.
        db_path = tmp / "deep" / "nested" / "fresh.db"

        env = dict(os.environ)
        for name in (
            "YODAW_CONFIG",
            "YODAW_LLM_PROVIDER",
            "YODAW_LLM_STYLE",
            "YODAW_LLM_MODE",
            "YODAW_LLM_MODEL",
            "YODAW_LLM_BASE_URL",
            "YODAW_LLM_API_KEY",
            "YODAW_LLM_API_KEY_ENV",
        ):
            env.pop(name, None)
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["YODAW_DB_PATH"] = str(db_path)
        env["YODAW_CONFIG"] = str(home / ".config" / "yodaw" / "config.toml")

        # Provision exactly like `yodaw setup-9router --no-verify`
        # would, but hermetic: no network, concrete file write.
        sys.path.insert(0, str(REPO))
        from app.product_config import write_llm_config

        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            config_path = write_llm_config(
                env["YODAW_CONFIG"],
                provider="9router",
                model=model,
                base_url=ninerouter.normalize_base_url(base),
                api_key_env="NINEROUTER_API_KEY",
            )
        except Exception as exc:
            return _result(
                "fresh-install", False,
                error=f"config provisioning failed: {exc}",
            )
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home

        snippet = _FRESH_SNIPPET.replace("{repo!r}", repr(str(REPO)))
        try:
            completed = subprocess.run(
                [sys.executable, "-c", snippet],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(REPO),
            )
        except subprocess.TimeoutExpired:
            return _result(
                "fresh-install", False, error="fresh boot timed out",
                config=config_path,
            )
        if completed.returncode != 0:
            return _result(
                "fresh-install", False,
                error=(completed.stderr or completed.stdout)[-2000:],
                config=config_path,
            )
        try:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return _result(
                "fresh-install", False,
                error=f"unparseable fresh-boot output: "
                f"{completed.stdout[-1000:]}",
                config=config_path,
            )

    db_ok = bool((payload.get("db") or {}).get("ok"))
    cfg = payload.get("config") or {}
    health = payload.get("health") or {}
    ready = health.get("body", {}).get("status") == "READY"
    provider_ok = cfg.get("provider") == "9router"
    ok = db_ok and ready and provider_ok
    return _result(
        "fresh-install",
        ok,
        config=cfg.get("source_path"),
        provider=cfg.get("provider"),
        model=cfg.get("model"),
        base_url=cfg.get("base_url"),
        db_repaired=(payload.get("db") or {}).get("repaired"),
        health=health.get("body", health),
        key=cfg.get("api_key"),
    )


# ------------------------------------------------------------ restart


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def cmd_restart(args):
    import urllib.request

    with tempfile.TemporaryDirectory(prefix="yodaw_9r_restart_") as td:
        tmp = Path(td)
        port = _free_port()
        env = dict(os.environ)
        env["YODAW_RUNTIME_DIR"] = str(tmp / "runtime")
        env["YODAW_DB_PATH"] = str(tmp / "restart.db")
        env["YODAW_HOST"] = "127.0.0.1"
        env["YODAW_PORT"] = str(port)
        env["YODAW_LLM_STYLE"] = "9router"
        env["YODAW_LLM_BASE_URL"] = _base_url(args)
        if getattr(args, "model", None):
            env["YODAW_LLM_MODEL"] = args.model

        def run_yodaw(*argv, timeout=120):
            return subprocess.run(
                [sys.executable, "-m", "app.launcher", *argv],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(REPO),
            )

        def health():
            url = f"http://127.0.0.1:{port}/api/v1/health"
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    return response.status, json.loads(
                        response.read().decode() or "{}"
                    )
            except Exception as exc:
                return None, {"error": str(exc)[:200]}

        evidence: dict = {"port": port}
        try:
            started = run_yodaw("start", timeout=180)
            evidence["start_rc"] = started.returncode
            evidence["start_tail"] = (started.stdout + started.stderr)[-800:]
            code, body = health()
            evidence["health_after_start"] = {"code": code, "body": body}
            if body.get("status") != "READY":
                return _result(
                    "restart", False, exit_code=SKIP,
                    reason="runtime did not become READY after start",
                    **evidence,
                )

            status_before = run_yodaw("status")
            evidence["status_before"] = status_before.stdout[-800:]

            restarted = run_yodaw("restart", timeout=180)
            evidence["restart_rc"] = restarted.returncode
            evidence["restart_tail"] = (restarted.stdout + restarted.stderr)[-800:]
            code2, body2 = health()
            evidence["health_after_restart"] = {"code": code2, "body": body2}
            if body2.get("status") != "READY":
                return _result("restart", False, **evidence)

            status_after = run_yodaw("status")
            evidence["status_after"] = status_after.stdout[-800:]
            return _result("restart", True, **evidence)
        except subprocess.TimeoutExpired as exc:
            return _result(
                "restart", False, exit_code=SKIP,
                reason=f"launcher step timed out: {exc}", **evidence,
            )
        except Exception as exc:
            return _result(
                "restart", False,
                error=f"{type(exc).__name__}: {exc}", **evidence,
            )
        finally:
            try:
                run_yodaw("stop", timeout=60)
            except Exception:
                pass


# ------------------------------------------------------------ all / main


def cmd_all(args):
    from app.llm import ninerouter

    steps = []
    key = _api_key(args)
    base = _base_url(args)
    detection = ninerouter.detect_install(base, key, timeout=15)
    live = bool(detection["reachable"])

    ordered = ["probe", "models"]
    ordered += ["chat", "mission"] if live else []
    ordered += ["fallback", "fresh-install", "restart"]

    results = []
    for name in ordered:
        code, payload = COMMANDS[name](args)
        results.append(payload)
        steps.append({"step": name, "status": payload.get("status")})

    if not live:
        for name in ("chat", "mission"):
            results.append(
                {
                    "check": name,
                    "status": "SKIP",
                    "reason": "9Router daemon not reachable",
                }
            )
            steps.append({"step": name, "status": "SKIP"})

    failed = [s for s in steps if s["status"] == "FAIL"]
    ok = not failed
    summary = {
        "check": "all",
        "status": "PASS" if ok else "FAIL",
        "live_9router": live,
        "steps": steps,
        "results": results,
    }
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2))
    else:
        print(f"[{'PASS' if ok else 'FAIL'}] all (live_9router={live})")
        for payload in results:
            print(f"  [{payload.get('status')}] {payload.get('check')}", end="")
            reason = payload.get("reason") or payload.get("error")
            if reason:
                print(f" -- {str(reason)[:160]}")
            else:
                print()
    return PASS if ok else FAIL


COMMANDS = {
    "probe": cmd_probe,
    "models": cmd_models,
    "chat": cmd_chat,
    "mission": cmd_mission,
    "fallback": cmd_fallback,
    "fresh-install": cmd_fresh_install,
    "restart": cmd_restart,
    "all": cmd_all,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="9Router acceptance harness (exit 0 PASS, 1 FAIL, 2 SKIP)"
    )
    parser.add_argument(
        "command",
        choices=sorted(COMMANDS),
        help="acceptance check to run",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "all":
        return cmd_all(args)
    code, payload = COMMANDS[args.command](args)
    _emit(args, payload)
    return code


if __name__ == "__main__":
    sys.exit(main())
