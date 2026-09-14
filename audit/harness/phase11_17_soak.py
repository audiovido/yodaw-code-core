#!/usr/bin/env python
"""Phase 11 + 17: bounded soak - FD / SQLite / memory / worktree growth."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/user/yodaw-audit-work/harness")
from audit_lib import get, submit, wait_terminal, script_llm, reset_llm, edit_plan  # noqa: E402

REPO = "/home/user/yodaw-audit-work/fixture/repo"
WORKSPACE = Path("/home/user/yodaw-v1-audit/workspace")
DB = Path("/home/user/yodaw-audit-work/run/data/yodaw.db")
N = int(os.environ.get("SOAK_N", "40"))


def runtime_pid():
    out = subprocess.run(["pgrep", "-f", "app.runtime"], capture_output=True, text=True).stdout
    pids = [int(x) for x in out.split()]
    return pids[0] if pids else None


def fdcount(pid):
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except Exception:
        return -1


def db_fds(pid):
    try:
        n = 0
        for f in os.listdir(f"/proc/{pid}/fd"):
            try:
                if "yodaw.db" in os.readlink(f"/proc/{pid}/fd/{f}"):
                    n += 1
            except Exception:
                pass
        return n
    except Exception:
        return -1


def rss_kb(pid):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        pass
    return -1


def threads(pid):
    try:
        return int(open(f"/proc/{pid}/status").read().split("Threads:")[1].split()[0])
    except Exception:
        return -1


def git(*a):
    return subprocess.run(["git", "-C", REPO] + list(a), capture_output=True, text=True)


def snapshot(pid):
    ws = len([p for p in WORKSPACE.iterdir() if p.is_dir()]) if WORKSPACE.exists() else -1
    wts = len(git("worktree", "list").stdout.splitlines()) - 1
    brs = len([b for b in git("branch", "--format=%(refname:short)").stdout.split()
               if b.startswith("yodaw/")])
    dbsize = sum(p.stat().st_size for p in DB.parent.glob("yodaw.db*"))
    return {"fd": fdcount(pid), "dbfd": db_fds(pid), "rss_kb": rss_kb(pid),
            "threads": threads(pid), "workspace_dirs": ws, "worktrees": wts,
            "yodaw_branches": brs, "db_bytes": dbsize}


def main():
    pid = runtime_pid()
    print("runtime pid:", pid)
    if pid is None:
        print("runtime not running - aborting")
        return 1

    # start from a clean slate so growth is attributable to the soak
    git("worktree", "prune")
    for b in git("branch", "--format=%(refname:short)").stdout.split():
        if b.startswith("yodaw/"):
            git("branch", "-D", b)

    base = snapshot(pid)
    print("baseline:", base)

    plan = edit_plan("calc.py", "    return a - b", "    return a + b")
    reset_llm()
    script_llm([{"kind": "ok", "content": plan}])

    rows = []
    t0 = time.time()
    passed = failed = 0
    for i in range(N):
        st, _, m = submit(f"soak mission {i}", capability="repo-code", repo_path=REPO)
        s, _ = wait_terminal(m["id"], timeout=120)
        if s == "PASS":
            passed += 1
        else:
            failed += 1
            print(f"  mission {i} -> {s}")
        # NOTE: no manual cleanup here on purpose - the soak must measure
        # whatever the product itself leaves behind.
        if (i + 1) % 10 == 0:
            snap = snapshot(pid)
            rows.append((i + 1, snap))
            print(f"  after {i+1:3d} missions: fd={snap['fd']} dbfd={snap['dbfd']} "
                  f"rss={snap['rss_kb']}kB threads={snap['threads']} "
                  f"ws_dirs={snap['workspace_dirs']} db={snap['db_bytes']}B")

    end = snapshot(pid)
    dt = time.time() - t0
    print()
    print(f"soak: {N} missions in {dt:.1f}s ({dt/N:.2f}s each) passed={passed} failed={failed}")
    print("baseline:", base)
    print("final   :", end)
    print()
    for key in ("fd", "dbfd", "rss_kb", "threads", "workspace_dirs", "worktrees", "yodaw_branches", "db_bytes"):
        delta = end[key] - base[key]
        verdict = "OK" if key in ("db_bytes",) or delta <= 10 else "GROWTH"
        print(f"  {key:16s} {base[key]:>10} -> {end[key]:>10}  delta={delta:+d}  {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
