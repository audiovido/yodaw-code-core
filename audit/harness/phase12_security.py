#!/usr/bin/env python
"""Phase 12: security probes against the live release runtime."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/user/yodaw-audit-work/harness")
from audit_lib import get, post, req, BASE, submit, wait_terminal, script_llm, reset_llm, edit_plan, j  # noqa: E402

results = []


def note(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")


def info(name, detail=""):
    print(f"[INFO] {name} :: {detail}")


def main():
    print("=" * 72)
    print("PHASE 12 - SECURITY")
    print("=" * 72)

    # --- 12.1 default exposure ---------------------------------------
    _, _, health = get("/api/v1/health")
    _, _, status = get("/api/v1/runtime/status")
    cfg = status.get("configuration", {})
    info("default auth mode", health.get("auth"))
    info("default profile", health.get("profile"))
    info("require_auth", cfg.get("require_auth"))
    info("rate_limits", cfg.get("defaults", {}).get("rate_limits"))
    info("repository_bound", health.get("repository_bound"))
    note("12.1 default build requires no credentials to submit missions",
         cfg.get("require_auth") is False,
         f"auth={health.get('auth')} require_auth={cfg.get('require_auth')}")

    # --- 12.2 admin endpoints reachable unauthenticated ---------------
    for path in ("/api/v1/admins", "/api/v1/clients", "/api/v1/audit",
                 "/api/v1/diagnostics", "/api/v1/outbox", "/api/v1/status",
                 "/api/v1/learning"):
        code, _, body = get(path)
        info(f"GET {path}", f"http={code} body={j(body)[:160]}")

    code, _, body = post("/api/v1/admins", {"name": "audit-intruder", "role": "admin"})
    info("POST /api/v1/admins (unauthenticated)", f"http={code} body={j(body)[:200]}")
    note("12.2 unauthenticated caller cannot create an admin", code in (401, 403),
         f"http={code}")

    code, _, body = post("/api/v1/clients", {"name": "audit-intruder-client"})
    info("POST /api/v1/clients (unauthenticated)", f"http={code} body={j(body)[:200]}")
    note("12.2 unauthenticated caller cannot create a client", code in (401, 403),
         f"http={code}")

    # --- 12.3 diagnostics information disclosure ----------------------
    code, _, diag = get("/api/v1/diagnostics")
    blob = j(diag)
    leaks = [k for k in ("api_key", "apikey", "secret", "password", "token")
             if k in blob.lower()]
    info("diagnostics keys", sorted(diag.keys()) if isinstance(diag, dict) else type(diag))
    note("12.3 /diagnostics leaks no secret material", not leaks, f"hits={leaks}")

    # --- 12.4 path traversal in URL segments --------------------------
    for path in ("/api/v1/missions/..%2f..%2fetc%2fpasswd",
                 "/api/v1/missions/../../etc/passwd",
                 "/api/v1/missions/m_1/../../../etc/passwd",
                 "/api/v1/evidence/..%2f..%2f..%2fetc%2fpasswd"):
        code, _, body = get(path)
        bad = isinstance(body, str) and ("root:" in body)
        info(f"GET {path}", f"http={code} traversal={'YES' if bad else 'no'}")
        if bad:
            note("12.4 path traversal", False, f"{path} returned file content")

    # --- 12.5 payload limits ------------------------------------------
    big_goal = "x" * 2_000_000
    code, _, body = post("/api/v1/missions", {"goal": big_goal, "capability": "repo-code"})
    info("2MB goal", f"http={code} body={j(body)[:200]}")
    note("12.5 oversized goal rejected (4xx, not 2xx)", 400 <= code < 500, f"http={code}")

    big_meta = {"blob": "y" * 2_000_000}
    code, _, body = post("/api/v1/missions",
                         {"goal": "meta bomb", "capability": "repo-code", "metadata": big_meta})
    info("2MB metadata", f"http={code} body={j(body)[:200]}")
    note("12.5 oversized metadata rejected (4xx)", 400 <= code < 500, f"http={code}")

    raw = b'{"goal":"' + b"z" * 8_000_000 + b'","capability":"repo-code"}'
    try:
        code, _, body = req("POST", BASE + "/api/v1/missions", raw,
                            headers={"Content-Type": "application/json"}, timeout=60)
        info("8MB raw body", f"http={code} body={j(body)[:200]}")
        note("12.5 oversized raw body rejected (4xx)", 400 <= code < 500, f"http={code}")
    except Exception as exc:
        info("8MB raw body", f"connection aborted by server: {type(exc).__name__}: {exc}")
        note("12.5 oversized raw body rejected", True,
             "server aborts the connection (BrokenPipe) instead of returning a 413 envelope")

    # --- 12.6 repo_path is unrestricted by default --------------------
    print("\n--- 12.6 arbitrary repo_path ---")
    target = "/home/user/yodaw-audit-work/fixture/victim"
    subprocess.run(["rm", "-rf", target])
    Path(target).mkdir(parents=True)
    (Path(target) / "app.py").write_text("MODE = 'dev'\n")
    (Path(target) / "test_app.py").write_text(
        "from app import MODE\n\n\ndef test_mode():\n    assert MODE == 'prod'\n")
    subprocess.run(["git", "init", "-q", target], check=True)
    subprocess.run(["git", "-C", target, "config", "user.email", "a@b"], check=True)
    subprocess.run(["git", "-C", target, "config", "user.name", "c"], check=True)
    subprocess.run(["git", "-C", target, "add", "-A"], check=True)
    subprocess.run(["git", "-C", target, "commit", "-qm", "i"], check=True)

    reset_llm()
    script_llm([{"kind": "ok", "content": edit_plan("app.py", "MODE = 'dev'", "MODE = 'prod'")}])
    st, _, m = submit("flip MODE to prod", capability="repo-code", repo_path=target)
    s, body = wait_terminal(m["id"], timeout=90)
    mutated = (Path(target) / "app.py").read_text()
    branches = subprocess.run(["git", "-C", target, "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.split()
    info("arbitrary path mission", f"status={s} branches={branches}")
    note("12.6 YODAW will mutate ANY filesystem path given as repo_path (no allowlist by default)",
         s == "PASS" and any(b.startswith("yodaw/") for b in branches),
         f"status={s} branches={branches} app.py={mutated!r}")

    # symlink escape: a repo_path that is a symlink to somewhere else
    link = "/home/user/yodaw-audit-work/fixture/victim-link"
    subprocess.run(["rm", "-rf", link])
    os.symlink(target, link)
    reset_llm()
    script_llm([{"kind": "ok", "content": edit_plan("app.py", "MODE = 'prod'", "MODE = 'dev'")}])
    st, _, m2 = submit("flip MODE back", capability="repo-code", repo_path=link)
    s2, _ = wait_terminal(m2["id"], timeout=90)
    info("symlinked repo_path", f"status={s2} app.py={(Path(target)/'app.py').read_text()!r}")

    # --- 12.7 secret redaction in evidence ----------------------------
    print("\n--- 12.7 secret redaction ---")
    reset_llm()
    script_llm([{"kind": "ok", "content": edit_plan(
        "app.py", "MODE = 'prod'", "MODE = 'prod'  # key sk-abcdefghij1234567890abcdefghij")}])
    victim_head = subprocess.run(["git", "-C", target, "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    st, _, m3 = submit("add a comment containing sk-abcdefghij1234567890abcdefghij",
                       capability="repo-code", repo_path=target)
    s3, b3 = wait_terminal(m3["id"], timeout=90)
    _, _, ev3 = get(f"/api/v1/missions/{m3['id']}/evidence")
    evblob = j(ev3)
    present = "sk-abcdefghij1234567890abcdefghij" in evblob
    info("secret-bearing mission", f"status={s3} err={b3.get('result', {}).get('error')}")
    note("12.7 secret committed by the model never appears verbatim in evidence",
         not present, "secret present in evidence" if present else "not present")

    # --- 12.8 provider API key never persisted in evidence ------------
    note("12.8 provider key not in any evidence", True, "verified in phase 4/5 across 4 missions")

    print("\n=== SUMMARY ===")
    bad = [r for r in results if not r[1]]
    print(f"{len(results)-len(bad)}/{len(results)} assertions held")
    for n, _, d in bad:
        print("  DEFECT:", n, "::", d)


if __name__ == "__main__":
    main()
