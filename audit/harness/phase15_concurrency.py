#!/usr/bin/env python
"""Phase 15: concurrency, per-repo lease exclusion, and cancellation."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "/home/user/yodaw-audit-work/harness")
from audit_lib import get, post, submit, wait_terminal, script_llm, reset_llm, edit_plan, j  # noqa: E402

REPO = "/home/user/yodaw-audit-work/fixture/repo"
REPO2 = "/home/user/yodaw-audit-work/fixture/repo2"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo] + list(a), capture_output=True, text=True)


def reset(repo, sha=None):
    git(repo, "worktree", "prune")
    git(repo, "checkout", "-q", "master")
    if sha:
        git(repo, "reset", "-q", "--hard", sha)
    git(repo, "clean", "-qfd")
    out = git(repo, "branch", "--format=%(refname:short)")
    for b in out.stdout.split():
        if b.startswith("yodaw/"):
            git(repo, "branch", "-D", b)


def make_repo2():
    if Path(REPO2).exists():
        subprocess.run(["rm", "-rf", REPO2])
    Path(REPO2).mkdir(parents=True)
    (Path(REPO2) / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (Path(REPO2) / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    git(REPO2, "init", "-q")
    git(REPO2, "config", "user.email", "audit@arena")
    git(REPO2, "config", "user.name", "Arena Audit")
    git(REPO2, "add", "-A")
    git(REPO2, "commit", "-qm", "initial fixture 2")
    return git(REPO2, "rev-parse", "HEAD").stdout.strip()


def inflight():
    s, _, b = get("/api/v1/runtime/status")
    try:
        return int(b["coordinator"]["inflight"])
    except Exception:
        return -1


def main():
    reset(REPO, "c1705e7285112e6e8df380d19989c3a22fd9a559")
    sha2 = make_repo2()
    print("repo2 at", sha2)

    plan = edit_plan("calc.py", "    return a - b", "    return a + b")

    # ---------- 15.1 six missions on the SAME repo -------------------
    print("\n--- 15.1 six concurrent missions against one repo (lease exclusion) ---")
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 2.0, "content": plan}])
    ids = []
    for i in range(6):
        st, _, m = submit(f"concurrent same-repo mission {i}", capability="repo-code", repo_path=REPO)
        ids.append(m["id"])
    print("submitted", ids)

    peak = 0
    samples = []
    deadline = time.time() + 240
    done = set()
    while time.time() < deadline and len(done) < len(ids):
        n = inflight()
        samples.append(n)
        peak = max(peak, n)
        for mid in ids:
            if mid in done:
                continue
            s, _, b = get(f"/api/v1/missions/{mid}")
            if b.get("status") in ("PASS", "FAIL", "CANCELLED", "BLOCKED_EXTERNAL"):
                done.add(mid)
        time.sleep(0.2)

    statuses = {}
    for mid in ids:
        s, _, b = get(f"/api/v1/missions/{mid}")
        statuses[mid] = b.get("status")
    print("statuses:", json.dumps(statuses))
    print("inflight samples (max observed):", peak)
    check("15.1 all six reached a terminal state", len(done) == 6, f"done={len(done)} {statuses}")
    check("15.1 inflight never exceeded YODAW_MAX_CONCURRENT_MISSIONS (2)",
          peak <= 2, f"peak={peak}")

    commits = git(REPO, "log", "--all", "--format=%H").stdout.split()
    passed = sum(1 for v in statuses.values() if v == "PASS")
    ncommits = len(set(commits)) - 1
    check("15.1 one commit per PASS, no duplicate/lost commits",
          ncommits == passed,
          f"commits={ncommits} passed={passed}")
    fsck = git(REPO, "fsck", "--no-progress")
    check("15.1 target repo not corrupted (git fsck clean)", "",
          (fsck.stdout + fsck.stderr).strip()[:200])

    # ---------- 15.2 two different repos in parallel -----------------
    print("\n--- 15.2 two different repos at once (should overlap) ---")
    reset(REPO, "c1705e7285112e6e8df380d19989c3a22fd9a559")
    reset(REPO2, sha2)
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 4.0, "content": plan}])
    _, _, a = submit("parallel repo A", capability="repo-code", repo_path=REPO)
    _, _, b = submit("parallel repo B", capability="repo-code", repo_path=REPO2)
    saw_two = False
    deadline = time.time() + 120
    while time.time() < deadline:
        n = inflight()
        if n >= 2:
            saw_two = True
        sa = get(f"/api/v1/missions/{a['id']}")[1].get("status")
        sb = get(f"/api/v1/missions/{b['id']}")[1].get("status")
        if sa in ("PASS", "FAIL", "CANCELLED", "BLOCKED_EXTERNAL") and \
           sb in ("PASS", "FAIL", "CANCELLED", "BLOCKED_EXTERNAL"):
            break
        time.sleep(0.2)
    check("15.2 different repos run concurrently (inflight reached 2)", saw_two,
          f"saw_two={saw_two}")
    print("    repo A ->", sa, " repo B ->", sb)

    # ---------- 15.3 cancellation mid-flight -------------------------
    print("\n--- 15.3 cancel a mission while it is executing ---")
    reset(REPO, "c1705e7285112e6e8df380d19989c3a22fd9a559")
    before = git(REPO, "rev-parse", "HEAD").stdout.strip()
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 12.0, "content": plan}])
    _, _, c = submit("cancel me", capability="repo-code", repo_path=REPO)
    # wait until executing
    for _ in range(80):
        if get(f"/api/v1/missions/{c['id']}")[1].get("status") in ("EXECUTING", "RUNNING", "PLANNING"):
            break
        time.sleep(0.25)
    print("    status before cancel:", get(f"/api/v1/missions/{c['id']}")[1].get("status"))
    st, _, cr = post(f"/api/v1/missions/{c['id']}/cancel", {})
    print("    cancel http:", st, j(cr)[:200])
    final, body = wait_terminal(c["id"], timeout=120)
    check("15.3 cancelled mission is CANCELLED", final == "CANCELLED",
          f"final={final} err={body.get('result', {}).get('error')}")
    after = git(REPO, "rev-parse", "HEAD").stdout.strip()
    check("15.3 cancelled mission produced no commit on master", before == after,
          f"{before[:8]} vs {after[:8]}")
    brs = [x for x in git(REPO, "branch", "--format=%(refname:short)").stdout.split()
           if x.startswith("yodaw/")]
    print("    leftover yodaw/* branches:", len(brs))

    # ---------- 15.4 cancel a QUEUED mission --------------------------
    print("\n--- 15.4 cancel while queued ---")
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 8.0, "content": plan}])
    _, _, q1 = submit("busy", capability="repo-code", repo_path=REPO)
    time.sleep(0.5)
    # saturate both slots
    for i in range(3):
        submit(f"filler {i}", capability="repo-code", repo_path=REPO2)
    time.sleep(0.5)
    _, _, q2 = submit("queued victim", capability="repo-code", repo_path=REPO2)
    st_q = get(f"/api/v1/missions/{q2['id']}")[1].get("status")
    print("    queued victim status:", st_q)
    post(f"/api/v1/missions/{q2['id']}/cancel", {})
    final2, body2 = wait_terminal(q2["id"], timeout=60)
    check("15.4 queued mission cancels immediately", final2 == "CANCELLED",
          f"final={final2} (was {st_q})")

    print("\n=== SUMMARY ===")
    bad = [r for r in results if not r[1]]
    print(f"{len(results)-len(bad)}/{len(results)} assertions held")
    for n, _, d in bad:
        print("  DEFECT:", n, "::", d)


if __name__ == "__main__":
    main()
