#!/usr/bin/env python
"""Phase 15 (corrected): parallel repos + cancellation, with the harness bug fixed.

The first pass of phase15_concurrency.py read response HEADERS instead of the
JSON body (`get(path)[1]` instead of `get(path)[2]`), so it never observed
EXECUTING and issued cancels too late. This version uses an explicit
`mstatus()` helper and prints the raw tuple shape so the mistake cannot repeat.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/user/yodaw-audit-work/harness")
from audit_lib import get, post, submit, wait_terminal, script_llm, reset_llm, edit_plan, j  # noqa: E402

REPO = "/home/user/yodaw-audit-work/fixture/repo"
REPO2 = "/home/user/yodaw-audit-work/fixture/repo2"
TERMINAL = ("PASS", "FAIL", "CANCELLED", "BLOCKED_EXTERNAL")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")


def mstatus(mid):
    status_code, headers, body = get(f"/api/v1/missions/{mid}")
    assert isinstance(body, dict), f"body is {type(body)}: {body!r}"
    return body.get("status"), body


def git(repo, *a):
    return subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True)


def reset(repo, sha):
    git(repo, "worktree", "prune")
    git(repo, "checkout", "-q", "master")
    git(repo, "reset", "-q", "--hard", sha)
    git(repo, "clean", "-qfd")
    for b in git(repo, "branch", "--format=%(refname:short)").stdout.split():
        if b.startswith("yodaw/"):
            git(repo, "branch", "-D", b)


def inflight():
    _, _, b = get("/api/v1/runtime/status")
    return int(b["coordinator"]["inflight"])


def main():
    sha1 = git(REPO, "rev-parse", "HEAD").stdout.strip()
    sha2 = git(REPO2, "rev-parse", "HEAD").stdout.strip()
    print("repo1", sha1, " repo2", sha2)
    plan = edit_plan("calc.py", "    return a - b", "    return a + b")

    # ---------- 15.2 two different repos in parallel ------------------
    print("\n--- 15.2 two different repos at once ---")
    reset(REPO, sha1); reset(REPO2, sha2)
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 6.0, "content": plan}])
    _, _, a = submit("parallel repo A", capability="repo-code", repo_path=REPO)
    _, _, b = submit("parallel repo B", capability="repo-code", repo_path=REPO2)
    print("    submitted", a["id"], b["id"])
    saw_two = False
    peak = 0
    deadline = time.time() + 120
    while time.time() < deadline:
        n = inflight()
        peak = max(peak, n)
        if n >= 2:
            saw_two = True
        sa, _ = mstatus(a["id"])
        sb, _ = mstatus(b["id"])
        if sa in TERMINAL and sb in TERMINAL:
            break
        time.sleep(0.2)
    check("15.2 both missions terminal", sa in TERMINAL and sb in TERMINAL, f"A={sa} B={sb}")
    check("15.2 different repos ran concurrently (inflight peaked at 2)", saw_two,
          f"saw_two={saw_two} peak={peak}")

    # ---------- 15.3 cancellation mid-flight ---------------------------
    print("\n--- 15.3 cancel a mission while EXECUTING ---")
    reset(REPO, sha1)
    before = git(REPO, "rev-parse", "HEAD").stdout.strip()
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 20.0, "content": plan}])
    _, _, c = submit("cancel me while executing", capability="repo-code", repo_path=REPO)
    observed = None
    for _ in range(200):
        observed, _ = mstatus(c["id"])
        if observed in ("EXECUTING", "RUNNING"):
            break
        if observed in TERMINAL:
            break
        time.sleep(0.1)
    print("    observed status before cancel:", observed)
    check("15.3 mission was actually EXECUTING when cancelled", observed in ("EXECUTING", "RUNNING"),
          f"observed={observed}")
    t0 = time.time()
    code, _, cr = post(f"/api/v1/missions/{c['id']}/cancel", {})
    print("    cancel http:", code, j(cr)[:200])
    final, body = wait_terminal(c["id"], timeout=120)
    dt = time.time() - t0
    print(f"    terminal={final} after {dt:.1f}s err={body.get('result', {}).get('error')}")
    check("15.3 executing mission ends CANCELLED", final == "CANCELLED",
          f"final={final} err={body.get('result', {}).get('error')}")
    after = git(REPO, "rev-parse", "HEAD").stdout.strip()
    check("15.3 cancelled mission produced no commit on master", before == after,
          f"{before[:8]} vs {after[:8]}")
    brs = [x for x in git(REPO, "branch", "--format=%(refname:short)").stdout.split() if x.startswith("yodaw/")]
    wts = [l for l in git(REPO, "worktree", "list").stdout.splitlines()[1:]]
    print(f"    leftover yodaw/* branches: {len(brs)}; leftover worktrees: {len(wts)}")
    check("15.3 no leaked worktree after cancellation", len(wts) == 0, str(wts))
    check("15.3 no leaked task branch after cancellation", len(brs) == 0, str(brs))

    # ---------- 15.5 cancel an already-finished mission ----------------
    print("\n--- 15.5 cancel an already-finished mission ---")
    code, _, cr = post(f"/api/v1/missions/{a['id']}/cancel", {})
    check("15.5 cancelling a finished mission is refused with 409", code == 409, f"http={code}")
    st_after, _ = mstatus(a["id"])
    check("15.5 finished mission status unchanged by the refused cancel", st_after == "PASS",
          f"status={st_after}")

    # ---------- 15.6 cancel an unknown mission -------------------------
    print("\n--- 15.6 cancel an unknown mission id ---")
    code, _, cr = post("/api/v1/missions/m_does_not_exist/cancel", {})
    check("15.6 unknown mission -> 404", code == 404, f"http={code} body={j(cr)[:150]}")

    print("\n=== SUMMARY ===")
    bad = [r for r in results if not r[1]]
    print(f"{len(results)-len(bad)}/{len(results)} assertions held")
    for n, _, d in bad:
        print("  DEFECT:", n, "::", d)


if __name__ == "__main__":
    main()
