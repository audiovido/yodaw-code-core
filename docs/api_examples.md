# YODAW API — Example Requests & Responses

All examples use base `http://127.0.0.1:8844`. Adjust `YODAW_BASE` as needed.

---

## 1. Health / Readiness

**Request:**
```bash
curl -s http://127.0.0.1:8844/api/v1/health
```

**Response (200):**
```json
{
  "service": "YODAW",
  "status": "READY",
  "auth": "local-dev",
  "workers": [
    {"name": "code-bud", "status": "READY", "capabilities": ["code"]},
    {"name": "repo-code-bud", "status": "READY", "capabilities": ["repo-code"]}
  ],
  "config_ok": true,
  "config_error": null,
  "profile": "local"
}
```

---

## 2. Capabilities

**Request:**
```bash
curl -s http://127.0.0.1:8844/api/v1/capabilities
```

**Response (200):**
```json
{
  "capabilities": [
    {"name": "code-bud", "capabilities": ["code"]},
    {"name": "repo-code-bud", "capabilities": ["repo-code"]}
  ],
  "mission_statuses": [
    "QUEUED", "OBSERVING", "PLANNING", "EXECUTING",
    "VERIFYING", "RECOVERING", "PASS", "FAIL",
    "BLOCKED_EXTERNAL", "CANCELLED"
  ]
}
```

---

## 3. Submit Mission (code capability)

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{"goal":"Add retry logic to sync client","capability":"code"}'
```

**Response (200):**
```json
{
  "mission_id": "m_abc123def456",
  "id": "m_abc123def456",
  "status": "QUEUED",
  "created_at": "2026-09-11T10:00:00.123456+00:00",
  "updated_at": "2026-09-11T10:00:00.123456+00:00",
  "attempt": 0,
  "attempts": 1,
  "error_class": null,
  "result": {},
  "replayed": false,
  "links": {
    "self": "/api/v1/missions/m_abc123def456",
    "evidence": "/api/v1/missions/m_abc123def456/evidence",
    "cancel": "/api/v1/missions/m_abc123def456/cancel",
    "retry": "/api/v1/missions/m_abc123def456/retry"
  }
}
```

---

## 4. Submit Mission (repo-code with repo_path)

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Refactor greet() to use f-string",
    "capability": "repo-code",
    "repo_path": "/home/user/myproject",
    "repo_ref": "myorg/myproject@abc123"
  }'
```

**Response (200):**
```json
{
  "mission_id": "m_xyz789",
  "id": "m_xyz789",
  "status": "QUEUED",
  "created_at": "2026-09-11T10:01:00.123456+00:00",
  "updated_at": "2026-09-11T10:01:00.123456+00:00",
  "attempt": 0,
  "attempts": 1,
  "error_class": null,
  "result": {},
  "replayed": false,
  "links": {
    "self": "/api/v1/missions/m_xyz789",
    "evidence": "/api/v1/missions/m_xyz789/evidence",
    "cancel": "/api/v1/missions/m_xyz789/cancel",
    "retry": "/api/v1/missions/m_xyz789/retry"
  }
}
```

---

## 5. Poll Mission Status

**Request:**
```bash
curl -s http://127.0.0.1:8844/api/v1/missions/m_abc123def456
```

**Response while running (200):**
```json
{
  "mission_id": "m_abc123def456",
  "id": "m_abc123def456",
  "status": "EXECUTING",
  "created_at": "2026-09-11T10:00:00.123456+00:00",
  "updated_at": "2026-09-11T10:00:15.789012+00:00",
  "attempt": 0,
  "attempts": 1,
  "worker": "code-bud",
  "started_at": "2026-09-11T10:00:05.123456+00:00",
  "heartbeat_at": "2026-09-11T10:00:15.789012+00:00",
  "error_class": null,
  "result": {},
  "metadata": {"product_attempts": 1, "max_retries": 2},
  "evidence": [],
  "links": {...}
}
```

**Response at terminal PASS (200):**
```json
{
  "mission_id": "m_abc123def456",
  "id": "m_abc123def456",
  "status": "PASS",
  "created_at": "2026-09-11T10:00:00.123456+00:00",
  "updated_at": "2026-09-11T10:00:45.123456+00:00",
  "attempt": 0,
  "attempts": 1,
  "worker": "code-bud",
  "started_at": "2026-09-11T10:00:05.123456+00:00",
  "finished_at": "2026-09-11T10:00:45.123456+00:00",
  "error_class": null,
  "result": {
    "goal": "Add retry logic to sync client",
    "workspace": "/path/to/workdir",
    "tests_passed": true,
    "commit_sha": "a1b2c3d4",
    "working_tree_clean": true
  },
  "evidence": [...],
  "links": {...}
}
```

---

## 6. Get Evidence

**Request:**
```bash
curl -s http://127.0.0.1:8844/api/v1/missions/m_abc123def456/evidence
```

**Response (200):**
```json
{
  "mission_id": "m_abc123def456",
  "evidence": [
    {
      "cmd": "git init",
      "cwd": "/workspace/yodaw_abc123",
      "stdout": "Initialized empty Git repository in /workspace/yodaw_abc123/.git/\n",
      "stderr": "",
      "returncode": 0,
      "timestamp": "2026-09-11T10:00:05.123456+00:00"
    },
    {
      "cmd": "git config user.email yodaw@local",
      "cwd": "/workspace/yodaw_abc123",
      "stdout": "",
      "stderr": "",
      "returncode": 0,
      "timestamp": "2026-09-11T10:00:05.124456+00:00"
    },
    {
      "cmd": "/usr/local/bin/python3.12 -m pytest -q",
      "cwd": "/workspace/yodaw_abc123",
      "stdout": "2 passed in 0.12s\n",
      "stderr": "",
      "returncode": 0,
      "timestamp": "2026-09-11T10:00:40.123456+00:00"
    },
    {
      "cmd": "git diff",
      "cwd": "/workspace/yodaw_abc123",
      "stdout": "+def multiply(a, b):\n+    return a * b\n",
      "stderr": "",
      "returncode": 0,
      "timestamp": "2026-09-11T10:00:42.123456+00:00"
    }
    // ... more items
  ]
}
```

---

## 7. Cancel Mission (QUEUED)

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions/m_abc123def456/cancel \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response (200):**
```json
{
  "id": "m_abc123def456",
  "status": "CANCELLED"
}
```

---

## 8. Cancel Mission (RUNNING)

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions/m_running123/cancel \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response (200):**
```json
{
  "id": "m_running123",
  "status": "CANCELLING",
  "detail": "cancellation requested; worker will stop at the next checkpoint"
}
```

---

## 9. Cancel Mission (Already Finished)

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions/m_finished456/cancel \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response (409):**
```json
{
  "detail": "Mission already finished"
}
```

---

## 10. Retry Mission (BLOCKED)

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions/m_blocked789/retry \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response (200):**
```json
{
  "mission_id": "m_new123",
  "retried_from": "m_blocked789",
  "status": "QUEUED",
  "links": {
    "self": "/api/v1/missions/m_new123",
    "evidence": "/api/v1/missions/m_new123/evidence"
  }
}
```

---

## 11. Retry Mission (PASS - not allowed)

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions/m_pass123/retry \
  -H "Content-Type: application/json" \
  -d '{}'
```

**Response (409):**
```json
{
  "detail": "mission is not in a retryable terminal state"
}
```

---

## 12. Unknown Mission

**Request:**
```bash
curl -s http://127.0.0.1:8844/api/v1/missions/m_doesnotexist
```

**Response (404):**
```json
{
  "detail": "Mission not found"
}
```

---

## 13. Unknown Capability → BLOCKED

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{"goal":"Impossible task","capability":"nonexistent"}'
```

**Response (200):**
```json
{
  "mission_id": "m_blocked123",
  "id": "m_blocked123",
  "status": "BLOCKED",
  "created_at": "2026-09-11T10:05:00.123456+00:00",
  "updated_at": "2026-09-11T10:05:00.123456+00:00",
  "attempt": 0,
  "attempts": 1,
  "error_class": "task",
  "result": {
    "error": "No worker for capability: nonexistent"
  },
  "links": {...},
  "detail": "no worker for capability; mission blocked"
}
```

---

## 14. Idempotent Submit

**Request (first):**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-unique-key-123" \
  -d '{"goal":"Idempotent test","capability":"code"}'
```

**Response (200):**
```json
{ "mission_id": "m_first123", "status": "QUEUED", "replayed": false, ... }
```

**Request (replay same key):**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: my-unique-key-123" \
  -d '{"goal":"Idempotent test","capability":"code"}'
```

**Response (200):**
```json
{ "mission_id": "m_first123", "status": "QUEUED", "replayed": true, ... }
```

---

## 15. Rate Limited (429)

**Request:**
```bash
for i in {1..100}; do curl -X POST ...; done
```

**Response (429):**
```json
{
  "detail": "rate limit exceeded"
}
```
Headers:
```
Retry-After: 2
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
```

---

## 16. Dry Run

**Request:**
```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{"goal":"dry run test","capability":"code","dry_run":true}'
```

**Response (200):**
```json
{
  "mission_id": "m_dry123",
  "status": "PASS",
  "result": {
    "dry_run": true,
    "goal": "dry run test",
    "repo_context": {...},
    "plan": {...},
    "skills": {...}
  },
  "error_class": "task"
}
```