#!/usr/bin/env python
"""Adversarial stub OpenAI-compatible LLM gateway for the YODAW v1.0.0 audit.

This is a DIAGNOSTIC tool used by the deep red-team audit. It is not
production code and never ships.

Capabilities:
  * GET  /v1/models           -> OpenAI-style model list (+ bare "combo" ids)
  * POST /v1/chat/completions -> scripted responses with fault injection
  * POST /__audit/script      -> replace the response script (JSON list)
  * GET  /__audit/state       -> counters, last request bodies/headers
  * POST /__audit/reset       -> clear state

Script entry shapes (consumed in order; the LAST entry repeats forever
unless "once": true entries are exhausted):

  {"kind": "ok", "content": "<assistant text>"}
  {"kind": "status", "code": 500, "body": "..."}
  {"kind": "delay", "seconds": 3.0, "content": "..."}
  {"kind": "raw", "body": "<exact bytes>", "content_type": "...", "code": 200}
  {"kind": "sse", "chunks": ["a", "b"], "trailer": "data: [DONE]"}
  {"kind": "sse_partial", "chunks": ["{\"choices\""], "truncate": true}
  {"kind": "hang", "seconds": 60}      # drip forever, no close
  {"kind": "close"}                    # hard-close the socket mid-body
  {"kind": "huge", "bytes": 1000000}   # unbounded-size body test

Every request is recorded (method, path, headers with Authorization
masked only in the audit view, raw body length) so secret-leak and
prompt-content assertions can be made from evidence.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

STATE_LOCK = threading.Lock()
STATE = {
    "script": [],
    "cursor": 0,
    "requests": [],
    "chat_calls": 0,
    "models_calls": 0,
}

HOST = os.environ.get("STUB_LLM_HOST", "127.0.0.1")
PORT = int(os.environ.get("STUB_LLM_PORT", "9911"))
RECORD_BODY = os.environ.get("STUB_LLM_RECORD_BODY", "1") == "1"


def _next_step():
    with STATE_LOCK:
        script = STATE["script"]
        if not script:
            return {"kind": "ok", "content": json.dumps({"action": "blocked", "reason": "no script"})}
        idx = STATE["cursor"]
        step = script[min(idx, len(script) - 1)]
        if idx < len(script) - 1:
            STATE["cursor"] = idx + 1
        return step


def _mask(headers):
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-api-key", "x-9r-cli-token"):
            out[k] = v[:12] + "...<REDACTED-BY-HARNESS>"
        else:
            out[k] = v
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence
        pass

    def _record(self, body: bytes):
        with STATE_LOCK:
            entry = {
                "at": time.time(),
                "method": self.command,
                "path": self.path,
                "headers": _mask(dict(self.headers)),
                "raw_authorization_present": "authorization" in {k.lower() for k in self.headers},
                "body_len": len(body),
            }
            if RECORD_BODY:
                try:
                    entry["body"] = json.loads(body.decode("utf-8"))
                except Exception:
                    entry["body"] = body.decode("utf-8", "replace")[:4000]
            STATE["requests"].append(entry)
            if len(STATE["requests"]) > 200:
                STATE["requests"] = STATE["requests"][-200:]

    def _json(self, code, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record(b"")
        if self.path.startswith("/v1/models"):
            with STATE_LOCK:
                STATE["models_calls"] += 1
            self._json(200, {"object": "list", "data": [
                {"id": "stub/model-a", "object": "model"},
                {"id": "stub/model-b", "object": "model"},
                {"id": "mycombo", "object": "model"},
                {"id": "premium-coding", "object": "model"},
            ]})
            return
        if self.path.startswith("/__audit/state"):
            with STATE_LOCK:
                snap = json.loads(json.dumps(STATE))
            self._json(200, snap)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)

        if self.path.startswith("/__audit/script"):
            try:
                script = json.loads(body.decode())
            except Exception as exc:
                self._json(400, {"error": str(exc)})
                return
            with STATE_LOCK:
                STATE["script"] = script if isinstance(script, list) else [script]
                STATE["cursor"] = 0
            self._json(200, {"ok": True, "len": len(STATE["script"])})
            return

        if self.path.startswith("/__audit/reset"):
            with STATE_LOCK:
                STATE["script"] = []
                STATE["cursor"] = 0
                STATE["requests"] = []
                STATE["chat_calls"] = 0
                STATE["models_calls"] = 0
            self._json(200, {"ok": True})
            return

        if self.path.startswith("/v1/chat/completions"):
            with STATE_LOCK:
                STATE["chat_calls"] += 1
            step = _next_step()
            self._serve_step(step)
            return

        self._json(404, {"error": "not found"})

    # ---- step behaviours -------------------------------------------------
    def _serve_step(self, step):
        kind = step.get("kind", "ok")

        if kind == "ok":
            self._json(200, {
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "model": step.get("model", "stub/model-a"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": step.get("content", "")},
                    "finish_reason": step.get("finish_reason", "stop"),
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
            return

        if kind == "status":
            payload = step.get("body")
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload).encode()
            else:
                payload = str(payload if payload is not None else "error").encode()
            self.send_response(int(step.get("code", 500)))
            self.send_header("Content-Type", step.get("content_type", "text/plain"))
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if kind == "delay":
            time.sleep(float(step.get("seconds", 1.0)))
            self._serve_step({**step, "kind": "ok"})
            return

        if kind == "raw":
            payload = step["body"].encode() if isinstance(step["body"], str) else bytes(step["body"])
            self.send_response(int(step.get("code", 200)))
            self.send_header("Content-Type", step.get("content_type", "application/json"))
            if step.get("content_length") is not None:
                self.send_header("Content-Length", str(int(step["content_length"])))
            else:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if kind in ("sse", "sse_partial"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in step.get("chunks", []):
                self._write_chunk(f"data: {chunk}\n\n".encode())
                self.wfile.flush()
                time.sleep(float(step.get("inter_chunk_delay", 0)))
            trailer = step.get("trailer")
            if trailer is not None:
                self._write_chunk(trailer.encode())
                self.wfile.flush()
            if step.get("truncate"):
                # vanish mid-stream: no terminating chunk, socket closed
                try:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                               __import__("struct").pack("ii", 1, 0))
                    self.connection.close()
                except Exception:
                    pass
                self.close_connection = True
                return
            self._write_chunk(b"")  # terminate chunked body
            return

        if kind == "hang":
            # keep the connection open, drip a byte occasionally, never finish
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            deadline = time.time() + float(step.get("seconds", 60))
            try:
                while time.time() < deadline:
                    self._write_chunk(b": keepalive\n\n")
                    self.wfile.flush()
                    time.sleep(float(step.get("drip", 0.5)))
            except Exception:
                pass
            self.close_connection = True
            return

        if kind == "close":
            try:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                           __import__("struct").pack("ii", 1, 0))
                self.connection.close()
            except Exception:
                pass
            self.close_connection = True
            return

        if kind == "huge":
            n = int(step.get("bytes", 1_000_000))
            content = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "A" * n}}]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        self._json(400, {"error": f"unknown step kind {kind}"})

    def _write_chunk(self, data: bytes):
        self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    srv = Server((HOST, PORT), Handler)
    print(f"stub-llm listening on {HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
