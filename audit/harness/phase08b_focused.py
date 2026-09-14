#!/usr/bin/env python
"""Phase 8b/9b: focused proofs.

(a) streaming read timeout: a keepalive drip never trips the configured
    per-request timeout, so the only thing that ends the call is the
    server closing the socket.
(b) non-string assistant content is returned verbatim by the OpenAI
    style path (no isinstance check), unlike the 9Router path.
(c) worst-case wall-clock for one chat() at default settings.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, "/home/user/yodaw-v1-audit")
sys.path.insert(0, "/home/user/yodaw-audit-work/harness")

os.environ["YODAW_LLM_STYLE"] = "openai"
os.environ["YODAW_LLM_BASE_URL"] = "http://127.0.0.1:9911"
os.environ["YODAW_LLM_MODEL"] = "stub/model-a"
os.environ["YODAW_LLM_TIMEOUT_SECONDS"] = "3"
os.environ["YODAW_PROVIDER_BACKOFF_SECONDS"] = "0.05"
os.environ["YODAW_PROVIDER_MAX_RETRIES"] = "0"
os.environ["YODAW_LLM_STREAM"] = "1"

from audit_lib import script_llm, reset_llm  # noqa: E402
from app.llm.provider import LocalLLMProvider, LLMError, pop_attempt_log  # noqa: E402

p = LocalLLMProvider()

print("=== (a) keepalive drip vs a 3s read timeout, server drips for 30s ===")
reset_llm()
script_llm([{"kind": "hang", "seconds": 30, "drip": 0.4}])
t0 = time.time()
try:
    out = p.chat("sys", "user")
    print(f"  returned {out!r} after {time.time()-t0:.2f}s")
except LLMError as exc:
    print(f"  LLMError after {time.time()-t0:.2f}s: {str(exc)[:120]}")
except Exception as exc:
    print(f"  {type(exc).__name__} after {time.time()-t0:.2f}s: {str(exc)[:120]}")
elapsed = time.time() - t0
print(f"  configured timeout = 3s; call lasted {elapsed:.1f}s")
print(f"  VERDICT: {'READ TIMEOUT NEVER FIRED - call only ended when the server stopped' if elapsed > 20 else 'timeout fired'}")

print()
print("=== (b) non-string assistant content, stream OFF ===")
os.environ.pop("YODAW_LLM_STREAM", None)
p2 = LocalLLMProvider()
reset_llm()
script_llm([{"kind": "raw", "body": json.dumps({"choices": [{"message": {"content": 12345}}]}),
             "content_type": "application/json"}])
try:
    out = p2.chat("sys", "user")
    print(f"  chat() returned {out!r} (type {type(out).__name__})")
    print("  VERDICT: NO isinstance(str) validation on the OpenAI-style path")
    # what does the coder do with it?
    from app.llm.coder import generate_edit_plan
    import tempfile, subprocess
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], check=True)
        open(os.path.join(td, "a.py"), "w").write("x = 1\n")
        subprocess.run(["git", "-C", td, "add", "-A"], check=True)
        subprocess.run(["git", "-C", td, "-c", "user.email=a@b", "-c", "user.name=c",
                        "commit", "-qm", "i"], check=True)
        try:
            plan = generate_edit_plan("goal", td)
            print(f"  generate_edit_plan -> {type(plan).__name__}: {str(plan)[:200]}")
        except Exception as exc:
            print(f"  generate_edit_plan raised {type(exc).__name__}: {str(exc)[:160]}")
except Exception as exc:
    print(f"  {type(exc).__name__}: {str(exc)[:200]}")

print()
print("=== (c) worst-case wall clock for one chat() at SHIPPED defaults ===")
default_timeout = 1200
default_retries = 3
print(f"  shipped YODAW_LLM_TIMEOUT_SECONDS default = {default_timeout}")
print(f"  shipped YODAW_PROVIDER_MAX_RETRIES default = {default_retries}")
print(f"  attempts = {default_retries + 1}")
print(f"  worst case per LLM call = {(default_retries+1) * default_timeout / 60:.0f} minutes")
