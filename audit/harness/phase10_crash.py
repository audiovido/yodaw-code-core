#!/usr/bin/env python
"""Phase 10 part 1: kill the runtime mid-mission (SIGKILL) and snapshot state."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/user/yodaw-audit-work/harness")
from audit_lib import get, submit, wait_terminal, script_llm, reset_llm, edit_plan, j  # noqa: E402

REPO = "/home/user/yodaw-audit-work/fixture/repo"
DB = "/home/user/yodaw-audit-work/run/data/yodaw.db"


def runtime_pids():
    out = subprocess.run(["pgrep", "-f", "app.runtime"], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def fdcount(pid):
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except Exception:
        return -1


def main():
    pids = runtime_pids()
    print("runtime pids:", pids)
    print("fd counts:", {p: fdcount(p) for p in pids})

    # a slow provider response keeps the mission in EXECUTING long enough to kill
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 25,
                 "content": edit_plan("calc.py", "    return a - b", "    return a + b")}])

    st, _, m = submit("crash recovery probe", capability="repo-code", repo_path=REPO)
    mid = m["id"]
    print("submitted", mid, st)

    # wait for it to be claimed
    seen = None
    for _ in range(120):
        s, _, body = get(f"/api/v1/missions/{mid}")
        seen = body.get("status")
        if seen in ("EXECUTING", "PLANNING", "RUNNING", "OBSERVING", "VERIFYING"):
            break
        if seen in ("PASS", "FAIL", "CANCELLED", "BLOCKED_EXTERNAL"):
            break
        time.sleep(0.25)
    print("status just before kill:", seen)

    victims = runtime_pids()
    print("SIGKILL ->", victims)
    for p in victims:
        try:
            os.kill(p, signal.SIGKILL)
        except Exception as exc:
            print("  kill", p, "failed:", exc)
    time.sleep(2)
    print("survivors:", runtime_pids())

    # the API is now dead; read the mission straight out of SQLite
    import sqlite3
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("select id, status, updated_at from missions order by rowid desc limit 5").fetchall()
    print("\nSQLite state after SIGKILL:")
    for r in rows:
        print("  ", r["id"], r["status"], r["updated_at"])
    con.close()

    wt = subprocess.run(["git", "-C", REPO, "worktree", "list"], capture_output=True, text=True).stdout
    print("\ngit worktree list after crash:\n", wt)
    ws = Path("/home/user/yodaw-v1-audit/workspace")
    print("workspace dirs:", len([p for p in ws.iterdir() if p.is_dir()]))

    Path("/home/user/yodaw-audit-work/logs/phase10_crash_state.json").write_text(json.dumps({
        "mission_id": mid, "status_before_kill": seen, "killed": victims,
        "sqlite": [dict(r) for r in rows], "worktrees": wt,
    }, indent=2))


if __name__ == "__main__":
    main()
