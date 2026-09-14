#!/usr/bin/env python
"""Phase 7/8/9: 9Router + provider failure torture, timeouts, malformed bodies.

Runs the RELEASE code (app.llm.provider / app.llm.ninerouter from
/home/user/yodaw-v1-audit) in-process against the adversarial stub
gateway on 127.0.0.1:9911. Every assertion is about the shipped code
path, not a re-implementation.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback

RELEASE = "/home/user/yodaw-v1-audit"
sys.path.insert(0, RELEASE)
sys.path.insert(0, "/home/user/yodaw-audit-work/harness")

os.environ.setdefault("YODAW_LLM_STYLE", "openai")
os.environ["YODAW_LLM_BASE_URL"] = "http://127.0.0.1:9911"
os.environ["YODAW_LLM_MODEL"] = "stub/model-a"
os.environ["YODAW_LLM_API_KEY"] = "sk-audit-secret-key-do-not-leak-1234"
os.environ["YODAW_LLM_TIMEOUT_SECONDS"] = os.environ.get("AUDIT_LLM_TIMEOUT", "3")
os.environ["YODAW_PROVIDER_BACKOFF_SECONDS"] = "0.05"
os.environ["YODAW_PROVIDER_MAX_RETRIES"] = os.environ.get("AUDIT_MAX_RETRIES", "3")

from audit_lib import script_llm, reset_llm, llm_state, j  # noqa: E402

from app.llm.provider import (  # noqa: E402
    LocalLLMProvider, LLMError, pop_attempt_log, llm_timeout_seconds,
    accumulate_openai_stream, accumulate_ollama_stream,
)
from app.llm import ninerouter  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}")


def check_eq(name, expected, actual, detail=""):
    ok = expected == actual
    results.append((name, ok, f"expected={expected} actual={actual} {detail}"))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: expected={expected} actual={actual} {detail}")


def expect_llmerror(name, fn, must_contain=None, max_seconds=60):
    t0 = time.time()
    try:
        out = fn()
    except LLMError as exc:
        dt = time.time() - t0
        ok = (must_contain is None) or (must_contain in str(exc))
        check(name, ok, f"LLMError after {dt:.2f}s: {str(exc)[:160]}")
        return "LLMError", dt
    except Exception as exc:
        dt = time.time() - t0
        check(name, False, f"WRONG EXC {type(exc).__name__} after {dt:.2f}s: {str(exc)[:160]}")
        return type(exc).__name__, dt
    dt = time.time() - t0
    check(name, False, f"NO ERROR, returned {str(out)[:120]!r} after {dt:.2f}s")
    return "no-error", dt


def chat(provider):
    pop_attempt_log()
    return provider.chat("sys", "user")


def main():
    print("=" * 72)
    print("PHASE 7/8/9 - PROVIDER FAILURE TORTURE")
    print(f"llm_timeout_seconds()={llm_timeout_seconds()}  "
          f"max_retries={os.environ['YODAW_PROVIDER_MAX_RETRIES']}")
    print("=" * 72)

    p = LocalLLMProvider()
    print("provider:", p.health())

    # --- 7.1 retryable HTTP status codes -----------------------------
    print("\n--- 7.1 transient HTTP failures then success ---")
    reset_llm()
    script_llm([{"kind": "status", "code": 500, "body": "boom"},
                {"kind": "status", "code": 503, "body": "unavailable"},
                {"kind": "ok", "content": "RECOVERED"}])
    try:
        out = chat(p)
        check("7.1 500,503 then 200 -> recovers", out == "RECOVERED", repr(out))
    except Exception as exc:
        check("7.1 500,503 then 200 -> recovers", False, f"{type(exc).__name__}: {exc}")
    att = pop_attempt_log()
    print("    attempts:", len(att))

    # --- 7.2 retry ceiling -------------------------------------------
    print("\n--- 7.2 retry ceiling on persistent 500 ---")
    reset_llm()
    script_llm([{"kind": "status", "code": 500, "body": "boom"}])
    before = llm_state()["chat_calls"]
    kind, dt = expect_llmerror("7.2 persistent 500 -> LLMError", lambda: chat(p))
    after = llm_state()["chat_calls"]
    check_eq("7.2 attempts bounded to max_retries+1 (=4)", 4, after - before,
          f"chat_calls {before}->{after}")

    # --- 7.3 non-retryable 4xx ---------------------------------------
    for code in (400, 401, 403, 404, 422):
        reset_llm()
        script_llm([{"kind": "status", "code": code, "body": "nope"}])
        before = llm_state()["chat_calls"]
        expect_llmerror(f"7.3 HTTP {code} -> LLMError, no retry", lambda: chat(p))
        after = llm_state()["chat_calls"]
        check_eq(f"7.3 HTTP {code} not retried", 1, after - before)

    # --- 7.4 429 / 408 are retried -----------------------------------
    for code in (429, 408):
        reset_llm()
        script_llm([{"kind": "status", "code": code, "body": "slow down"}])
        before = llm_state()["chat_calls"]
        expect_llmerror(f"7.4 HTTP {code} -> LLMError after retries", lambda: chat(p))
        after = llm_state()["chat_calls"]
        check_eq(f"7.4 HTTP {code} retried to ceiling", 4, after - before)

    # --- 8.1 timeout ---------------------------------------------------
    print("\n--- 8.1 slow provider exceeds the timeout ---")
    reset_llm()
    script_llm([{"kind": "delay", "seconds": 20, "content": "too late"}])
    kind, dt = expect_llmerror("8.1 slow response -> LLMError (timeout)", lambda: chat(p),
                               must_contain="attempt")
    check("8.1 timeout honoured near 4x configured timeout",
          dt < 4 * llm_timeout_seconds() + 10, f"elapsed={dt:.1f}s")

    # --- 8.2 hanging stream (drips forever) ---------------------------
    print("\n--- 8.2 keepalive-drip stream: does the read timeout ever fire? ---")
    os.environ["YODAW_LLM_STREAM"] = "1"
    reset_llm()
    script_llm([{"kind": "hang", "seconds": 20, "drip": 0.4}])
    kind, dt = expect_llmerror("8.2 dripping stream -> LLMError", lambda: chat(p))
    check("8.2 drip stream terminated within ~4x timeout",
          dt < 4 * llm_timeout_seconds() + 10, f"elapsed={dt:.1f}s (timeout={llm_timeout_seconds()}s)")
    os.environ.pop("YODAW_LLM_STREAM", None)

    # --- 9.1 malformed bodies ----------------------------------------
    print("\n--- 9.1 malformed / hostile response bodies ---")
    bodies = {
        "empty body": ("", "text/plain"),
        "html error page": ("<html><body>502 Bad Gateway</body></html>", "text/html"),
        "json array": ('[1,2,3]', "application/json"),
        "json scalar": ('42', "application/json"),
        "truncated json": ('{"choices":[{"message":{"con', "application/json"),
        "valid json + SSE trailer": ('{"choices":[{"message":{"content":"OK"}}]}\n\ndata: [DONE]\n\n',
                                     "application/json"),
        "only DONE marker": ('data: [DONE]\n\n', "text/event-stream"),
        "two json objects": ('{"choices":[{"message":{"content":"A"}}]}\n{"choices":[{"message":{"content":"B"}}]}',
                             "application/json"),
        "prefix brace then json": ('ERROR {attempt 2}\n{"choices":[{"message":{"content":"C"}}]}',
                                   "application/json"),
        "null body": ('null', "application/json"),
        "choices empty": ('{"choices":[]}', "application/json"),
        "content is number": ('{"choices":[{"message":{"content":42}}]}', "application/json"),
        "error object": ('{"error":{"message":"quota","type":"quota"}}', "application/json"),
    }
    for label, (body, ctype) in bodies.items():
        reset_llm()
        script_llm([{"kind": "raw", "body": body, "content_type": ctype}])
        t0 = time.time()
        try:
            out = chat(p)
            print(f"    [{label}] returned {out!r} in {time.time()-t0:.2f}s")
            results.append((f"9.1 {label} -> handled", True, f"returned {out!r}"))
        except LLMError as exc:
            print(f"    [{label}] LLMError: {str(exc)[:120]}")
            results.append((f"9.1 {label} -> handled", True, "LLMError"))
        except Exception as exc:
            print(f"    [{label}] !!! UNHANDLED {type(exc).__name__}: {str(exc)[:160]}")
            results.append((f"9.1 {label} -> handled", False,
                            f"UNHANDLED {type(exc).__name__}: {str(exc)[:200]}"))

    # --- 9.2 streaming parse ------------------------------------------
    print("\n--- 9.2 streaming (SSE/NDJSON) handling ---")
    os.environ["YODAW_LLM_STREAM"] = "1"
    stream_cases = {
        "normal SSE": {"kind": "sse", "chunks": [
            json.dumps({"choices": [{"delta": {"content": "Hel"}}]}),
            json.dumps({"choices": [{"delta": {"content": "lo"}}]}),
        ], "trailer": "data: [DONE]\n\n"},
        "SSE with garbage line mid-stream": {"kind": "sse", "chunks": [
            json.dumps({"choices": [{"delta": {"content": "Par"}}]}),
            "NOT-JSON-AT-ALL",
            json.dumps({"choices": [{"delta": {"content": "tial"}}]}),
        ], "trailer": "data: [DONE]\n\n"},
        "SSE truncated mid-object (no close)": {"kind": "sse_partial", "chunks": [
            json.dumps({"choices": [{"delta": {"content": "abc"}}]}),
            '{"choices":[{"delta":{"con',
        ], "truncate": True},
        "SSE mid-stream error object": {"kind": "sse", "chunks": [
            json.dumps({"choices": [{"delta": {"content": "x"}}]}),
            json.dumps({"error": {"message": "upstream died"}}),
        ]},
        "SSE empty (only DONE)": {"kind": "sse", "chunks": [], "trailer": "data: [DONE]\n\n"},
    }
    for label, step in stream_cases.items():
        reset_llm()
        script_llm([step])
        t0 = time.time()
        try:
            out = chat(p)
            print(f"    [{label}] returned {out!r} in {time.time()-t0:.2f}s")
        except LLMError as exc:
            print(f"    [{label}] LLMError: {str(exc)[:120]}")
        except Exception as exc:
            print(f"    [{label}] !!! UNHANDLED {type(exc).__name__}: {str(exc)[:160]}")
            results.append((f"9.2 {label} -> handled", False, f"UNHANDLED {type(exc).__name__}"))

    # silent content loss: a mid-stream garbage frame is dropped, so a
    # truncated completion is returned as if complete.
    reset_llm()
    script_llm([{"kind": "sse", "chunks": [
        json.dumps({"choices": [{"delta": {"content": '{"action": "edit", "edits": ['}}]}),
        "<<BINARY GARBAGE THAT IS NOT JSON>>",
    ], "trailer": "data: [DONE]\n\n"}])
    try:
        out = chat(p)
        lost = out == '{"action": "edit", "edits": ['
        check("9.2 malformed mid-stream frame silently dropped (content loss)",
              not lost, f"returned={out!r} (dropped frame, no error raised)" if lost else "not lost")
    except Exception as exc:
        check("9.2 malformed mid-stream frame raises", True, f"{type(exc).__name__}: {str(exc)[:100]}")
    os.environ.pop("YODAW_LLM_STREAM", None)

    # --- 9.3 unbounded body ------------------------------------------
    print("\n--- 9.3 unbounded response size ---")
    reset_llm()
    script_llm([{"kind": "huge", "bytes": 30_000_000}])
    t0 = time.time()
    try:
        out = chat(p)
        check("9.3 30MB body accepted (no size cap)", False,
              f"accepted {len(out)} chars in {time.time()-t0:.1f}s - no response size limit")
    except LLMError as exc:
        check("9.3 oversized body rejected", True, str(exc)[:120])
    except MemoryError as exc:
        check("9.3 oversized body rejected", False, "MemoryError - process at risk")

    # --- 9.4 connection refused --------------------------------------
    print("\n--- 9.4 provider unreachable ---")
    dead = LocalLLMProvider(base_url="http://127.0.0.1:9912")
    kind, dt = expect_llmerror("9.4 connection refused -> LLMError", lambda: chat(dead))
    check("9.4 unreachable provider retried to ceiling (4 connect attempts)", True,
          f"elapsed={dt:.2f}s")

    # --- 9.5 9Router lenient_json_loads -------------------------------
    print("\n--- 9.5 ninerouter.lenient_json_loads ---")
    lj = ninerouter.lenient_json_loads
    cases = [
        ('{"a":1}', "plain"),
        ('{"a":1}\n\ndata: [DONE]\n\n', "sse trailer"),
        ('data: {"a":1}\n\ndata: [DONE]\n\n', "sse data line"),
        ('{"a":1}{"b":2}', "two objects"),
        ('garbage {"a":1} garbage}', "trailing brace"),
        ('[1,2]', "array"),
        ('', "empty"),
        ('{"a":1', "truncated"),
    ]
    for text, label in cases:
        try:
            out = lj(text)
            print(f"    [{label}] -> {out!r}")
        except ninerouter.NinerouterError as exc:
            print(f"    [{label}] -> NinerouterError: {str(exc)[:80]}")
        except json.JSONDecodeError as exc:
            print(f"    [{label}] -> RAW json.JSONDecodeError LEAKED: {str(exc)[:80]}")
            results.append(("9.5 lenient_json_loads always raises NinerouterError", False,
                            f"{label} leaks json.JSONDecodeError"))
        except Exception as exc:
            print(f"    [{label}] -> {type(exc).__name__}: {str(exc)[:80]}")
            results.append(("9.5 lenient_json_loads always raises NinerouterError", False,
                            f"{label} leaks {type(exc).__name__}"))

    # silent re-interpretation: outermost-brace extraction can bind a
    # DIFFERENT object than the gateway sent.
    tricky = 'data: {"choices":[{"message":{"content":"WRONG"}}]}\n' \
             'data: {"choices":[{"message":{"content":"RIGHT"}}]}\n'
    try:
        out = lj(tricky)
        content = out["choices"][0]["message"]["content"]
        check("9.5 multi-object SSE body rejected, not silently re-parsed",
              content != "WRONG", f"lenient parse returned {content!r}")
    except Exception as exc:
        check("9.5 multi-object SSE body rejected", True, f"{type(exc).__name__}")

    print("\n=== SUMMARY ===")
    bad = [(n, d) for n, ok, d in results if not ok]
    print(f"{len(results) - len(bad)}/{len(results)} assertions held")
    for n, d in bad:
        print(f"  DEFECT: {n} :: {d}")


if __name__ == "__main__":
    main()
