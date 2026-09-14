#!/usr/bin/env python
"""Phase 4 + 5: real public-API end-to-end mission lifecycle over HTTP.

Runs against the live YODAW runtime (v1.0.0, 333fd00) on 127.0.0.1:8844
and the deterministic stub provider on 127.0.0.1:9911.

IMPORTANT: the LLM is a scripted stub. This proves the *product serving
path* (API -> queue -> coordinator -> worker -> edit engine -> test run
-> commit -> evidence). It does NOT prove model-driven coding quality.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit_lib import (  # noqa: E402
    get, post, submit, wait_terminal, script_llm, reset_llm, llm_state,
    edit_plan, j,
)

REPO = "/home/user/yodaw-audit-work/fixture/repo"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")


def git(*args):
    return subprocess.run(["git", "-C", REPO] + list(args),
                          capture_output=True, text=True).stdout.strip()


def reset_fixture():
    subprocess.run(["git", "-C", REPO, "checkout", "-q", "--", "."], check=True)
    subprocess.run(["git", "-C", REPO, "clean", "-qfd"], check=True)


def main():
    reset_fixture()
    reset_llm()

    print("=== 0. health/version ===")
    st, _, health = get("/api/v1/health")
    check("health 200 READY", st == 200 and health.get("status") == "READY", j(health)[:300])
    st, _, ver = get("/api/v1/version")
    check("version reports release build sha", ver.get("build") == "333fd0060c381fa15193024e5436629d6d813ec0", j(ver))

    print("\n=== 1. real repo-code mission, scripted correct edit ===")
    script_llm([{"kind": "ok", "content": edit_plan("calc.py", "    return a - b", "    return a + b")}])
    before = git("rev-parse", "HEAD")
    st, _, mission = submit("Make add() add its arguments so the test passes",
                            capability="repo-code", repo_path=REPO)
    check("POST /missions -> QUEUED", st in (200, 201) and mission.get("status") == "QUEUED",
          f"http={st} id={mission.get('id')} status={mission.get('status')}")
    mid = mission.get("id")
    final_status, final = wait_terminal(mid, timeout=120)
    check("mission reached PASS", final_status == "PASS", f"status={final_status}")
    after = git("rev-parse", "HEAD")
    check("a new commit was created on the fixture repo", before != after, f"{before[:8]} -> {after[:8]}")
    check("fixture calc.py actually changed to a + b",
          "return a + b" in (Path(REPO) / "calc.py").read_text(),
          (Path(REPO) / "calc.py").read_text().replace("\n", "\\n"))
    check("fixture working tree clean after mission", git("status", "--porcelain") == "",
          repr(git("status", "--porcelain")))
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    print(f"    branch after mission: {branch}")
    st, _, ev = get(f"/api/v1/missions/{mid}/evidence")
    print("    evidence keys:", sorted(ev.keys()) if isinstance(ev, dict) else type(ev))
    evj = j(ev)
    check("evidence non-empty", isinstance(ev, dict) and len(evj) > 50, f"len={len(evj)}")
    check("evidence records tests_passed", '"tests_passed": true' in evj or '"tests_passed": True' in evj,
          evj[evj.find("tests_passed") - 40: evj.find("tests_passed") + 60] if "tests_passed" in evj else "NO tests_passed KEY")
    st, _, events = get(f"/api/v1/missions/{mid}/events")
    ev_types = [e.get("type") or e.get("event") for e in (events if isinstance(events, list) else events.get("events", []))]
    check("mission events recorded", len(ev_types) > 0, str(ev_types)[:300])
    check("provider attempt evidence captured", "provider_attempt" in evj, "provider_attempt" in evj)

    print("\n=== 2. blocked plan -> must NOT be PASS ===")
    reset_fixture()
    script_llm([{"kind": "ok", "content": json.dumps({"action": "blocked", "reason": "insufficient info"})}])
    b0 = git("rev-parse", "HEAD")
    st, _, m2 = submit("Do something impossible", capability="repo-code", repo_path=REPO)
    s2, f2 = wait_terminal(m2["id"], timeout=90)
    check("blocked plan -> not PASS", s2 != "PASS", f"status={s2} err={j(f2.get('error'))[:200] if isinstance(f2, dict) else ''}")
    check("blocked plan -> no new commit", git("rev-parse", "HEAD") == b0, "HEAD unchanged")

    print("\n=== 3. plan that breaks the tests -> must NOT be PASS ===")
    reset_fixture()
    script_llm([{"kind": "ok", "content": edit_plan("calc.py", "    return a - b", "    return a * b")}])
    b0 = git("rev-parse", "HEAD")
    st, _, m3 = submit("Break add", capability="repo-code", repo_path=REPO)
    s3, f3 = wait_terminal(m3["id"], timeout=150)
    check("failing-tests plan -> not PASS", s3 != "PASS", f"status={s3}")
    check("failing-tests plan -> no commit on main branch", git("rev-parse", "HEAD") == b0, "HEAD unchanged")

    print("\n=== 4. no-repo code mission -> must NOT be PASS (VERIFICATION_REPORT fix e58d608) ===")
    st, _, m4 = submit("just do code", capability="code")
    s4, f4 = wait_terminal(m4["id"], timeout=90)
    check("bare code capability without repo -> not PASS", s4 != "PASS", f"status={s4}")
    print("    detail:", j(f4)[:600])

    print("\n=== 5. secret leak check in evidence/logs ===")
    leak_haystack = []
    for mid_ in [mid, m2["id"], m3["id"], m4["id"]]:
        _, _, e = get(f"/api/v1/missions/{mid_}/evidence")
        leak_haystack.append(j(e))
        _, _, ev2 = get(f"/api/v1/missions/{mid_}/events")
        leak_haystack.append(j(ev2))
    blob = "\n".join(leak_haystack)
    check("API key NOT present in evidence/events", "sk-audit-secret-key-do-not-leak-1234" not in blob,
          "scanned evidence+events for all 4 missions")

    print("\n=== 6. stub provider saw the bearer key (proves real HTTP path) ===")
    state = llm_state()
    check("stub received chat calls", state["chat_calls"] >= 3, f"chat_calls={state['chat_calls']}")
    authed = [r for r in state["requests"] if r.get("path", "").startswith("/v1/chat") and r.get("raw_authorization_present")]
    check("YODAW sent Authorization to provider", len(authed) >= 1, f"authed={len(authed)}")

    print("\n=== SUMMARY ===")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"{passed}/{len(results)} checks passed")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} :: {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
