# YODAW API — External Client Usage (Minimal)

Canonical product surface: `/api/v1`

Base URL is configurable via env `YODAW_HOST` / `YODAW_PORT`. No source changes needed.

---

## Start Server

```bash
# Local development (SQLite, open access, embedded coordinator)
source .venv/bin/activate
python -m app.runtime

# Or with custom port/DB
YODAW_PORT=8844 YODAW_DB_PATH=/tmp/yodaw.db python -m app.runtime

# Health check
curl http://127.0.0.1:8844/api/v1/health
# {"service":"YODAW","status":"READY","auth":"local-dev","profile":"local",...}
```

**Required env (all optional with defaults):**

| Variable | Default | Purpose |
|----------|---------|---------|
| `YODAW_HOST` | `127.0.0.1` | Bind address |
| `YODAW_PORT` | `8844` | HTTP port |
| `YODAW_DB_PATH` | `data/yodaw.db` | SQLite path |
| `YODAW_PROFILE` | `local` | `local` \| `single-node` \| `multi-process` \| `production` |
| `YODAW_LOG_LEVEL` | `INFO` | Logging verbosity |

Production profiles (`single-node`, `multi-process`, `production`) require auth: set `YODAW_API_KEY` or create admin identities.

**Supported release scope:** trusted single-node operation only, verified end-to-end by Worker E
(see `docs/api_graduation_final_repo_code.md`). With no `YODAW_REPO_ROOTS` configured the
server accepts any local `repo_path`; set `YODAW_REPO_ROOTS` at startup to confine repo targets
(outside/`..`/symlink → 403). Broader or multi-tenant deployment is not proven.

---

## Quick Start (curl)

```bash
# Submit a mission
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{"goal":"Add retry logic to sync client","capability":"code"}'

# Response (QUEUED immediately)
# {"mission_id":"m_abc123","id":"m_abc123","status":"QUEUED",...}

# Poll for status
MISSION_ID=m_abc123
while true; do
  curl -s "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID" | jq .status
  sleep 2
done

# Get evidence
curl "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID/evidence"

# Cancel (if QUEUED)
curl -X POST "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID/cancel"

# Retry (terminal FAIL/BLOCKED_EXTERNAL/CANCELLED only)
curl -X POST "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID/retry"
```

---

## Python Client (JSON-only)

```python
import json
import urllib.request
import time

BASE = "http://127.0.0.1:8844"

def post(path, payload):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

def get(path):
    with urllib.request.urlopen(BASE + path) as r:
        return json.loads(r.read())

# 1. READINESS
health = get("/api/v1/health")
assert health["status"] == "READY"

# 2. SUBMIT
resp = post("/api/v1/missions", {"goal": "refactor greet() to f-string", "capability": "code"})
mid = resp["mission_id"]
print(f"Submitted {mid}: {resp['status']}")

# 3. POLL
while True:
    m = get(f"/api/v1/missions/{mid}")
    print(f"  {m['status']}")
    if m["status"] in ("PASS", "FAIL", "BLOCKED", "BLOCKED_EXTERNAL", "CANCELLED"):
        break
    time.sleep(0.5)

# 4. EVIDENCE
ev = get(f"/api/v1/missions/{mid}/evidence")
print(f"Evidence items: {len(ev['evidence'])}")

# 5. CANCEL / RETRY
# post(f"/api/v1/missions/{mid}/cancel", {})
# post(f"/api/v1/missions/{mid}/retry", {})
```

---

## API Reference (Minimal Surface)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/health` | Liveness + config (open) |
| GET | `/api/v1/status` | Service status + counts |
| GET | `/api/v1/capabilities` | Worker capabilities + statuses |
| POST | `/api/v1/missions` | Submit mission → returns `mission_id`, `QUEUED` |
| GET | `/api/v1/missions/{id}` | Mission detail + status |
| GET | `/api/v1/missions/{id}/evidence` | Structured evidence log |
| GET | `/api/v1/missions/{id}/events` | Internal event stream |
| POST | `/api/v1/missions/{id}/cancel` | Cancel (QUEUED→CANCELLED, RUNNING→CANCELLING) |
| POST | `/api/v1/missions/{id}/retry` | Retry terminal mission → new QUEUED |

**Authentication (local-dev):** none required. Production: `Authorization: Bearer <key>` header.

---

## Mission Lifecycle

```
QUEUED → RUNNING → PASS | FAIL | BLOCKED_EXTERNAL | CANCELLED
```

- `QUEUED`: enqueued, not yet claimed
- `RUNNING`: worker executing (includes observing/planning/executing/verifying/recovering)
- `PASS`: success
- `FAIL`: task error (validation, edits, tests)
- `BLOCKED_EXTERNAL`: provider/network fault (never a task FAIL)
- `CANCELLED`: cancelled by client

Terminal states: `PASS`, `FAIL`, `BLOCKED_EXTERNAL`, `CANCELLED`

Only `FAIL`, `BLOCKED_EXTERNAL`, `CANCELLED` may retry (max 3 product attempts).

---

## Mission Submit Body

```json
{
  "goal": "string (required)",
  "capability": "code | repo-code",
  "repo_path": "/abs/path/to/repo",
  "repo_ref": "owner/repo@sha",
  "constraints": {},
  "model": {"name": "preferred-model"},
  "provider": {"name": "preferred-provider"},
  "metadata": {},
  "dry_run": false,
  "idempotency_key": "unique-per-mission"
}
```

Only `goal` is required. `capability` defaults to `code`.

---

## Idempotency

- Header `Idempotency-Key` or body `idempotency_key`
- Replay returns original mission with `"replayed": true`

---

## Error Responses

| Code | Meaning |
|------|---------|
| 200 | Success (even for BLOCKED missions) |
| 400 | Invalid payload |
| 401 | Auth required (non-local profiles) |
| 403 | Forbidden (RBAC) |
| 404 | Mission not found (or cross-tenant) |
| 409 | Conflict (duplicate idempotency, retry not allowed, cancel finished) |
| 413 | Payload governance exceeded |
| 422 | Validation error |
| 429 | Rate limited (Retry-After, X-RateLimit-*) |

---

## Base URL Configuration

The client only needs the base URL. No code changes:

```bash
export YODAW_BASE="https://api.yodaw.example.com"
# or
export YODAW_BASE="http://staging.internal:8844"
```

---

## Shutdown

```bash
# SIGTERM drains inflight missions, stops claiming, closes DB
kill <pid>
# Or from Python
import signal, subprocess
proc = subprocess.Popen(...)
proc.send_signal(signal.SIGTERM)
proc.wait(timeout=25)
```