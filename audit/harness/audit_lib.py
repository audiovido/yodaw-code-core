#!/usr/bin/env python
"""Shared helpers for the YODAW v1.0.0 deep audit harness."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8844"
STUB = "http://127.0.0.1:9911"

TERMINAL = {"PASS", "FAIL", "BLOCKED_EXTERNAL", "CANCELLED", "COMPLETED", "FAILED"}


def req(method, url, body=None, headers=None, timeout=60, raw=False):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode()
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = body
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            payload = resp.read()
            status = resp.status
            rh = dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
        rh = dict(exc.headers or {})
    if raw:
        return status, rh, payload
    try:
        return status, rh, json.loads(payload.decode())
    except Exception:
        return status, rh, payload.decode("utf-8", "replace")


def get(path, **kw):
    return req("GET", BASE + path, **kw)


def post(path, body=None, **kw):
    return req("POST", BASE + path, body, **kw)


def script_llm(steps, base=STUB):
    return req("POST", base + "/__audit/script", steps)


def reset_llm(base=STUB):
    return req("POST", base + "/__audit/reset", {})


def llm_state(base=STUB):
    _, _, s = req("GET", base + "/__audit/state")
    return s


def edit_plan(target_file, find, replace, reason="audit scripted edit"):
    return json.dumps({
        "action": "edit",
        "edits": [{"target_file": target_file, "find": find, "replace": replace}],
        "reason": reason,
    })


def blocked_plan(reason="audit scripted block"):
    return json.dumps({"action": "blocked", "reason": reason})


def submit(goal, capability="repo_code", **extra):
    payload = {"goal": goal, "capability": capability}
    payload.update(extra)
    return post("/api/v1/missions", payload)


def wait_terminal(mission_id, timeout=180, interval=0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, _, body = get(f"/api/v1/missions/{mission_id}")
        last = body
        st = (body or {}).get("status") if isinstance(body, dict) else None
        if st in TERMINAL:
            return st, body
        time.sleep(interval)
    return (last or {}).get("status") if isinstance(last, dict) else None, last


def j(x):
    return json.dumps(x, indent=2, default=str)
