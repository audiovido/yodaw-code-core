# YODAW API — Production Graduation Checklist

**Worker E verification date:** 2026-09-11
**Branch:** yodaw/worker-api-e-e2e
**Base commit:** d89e8b26518d93df5d53939a1320e992e7d636b1 (origin/master)

---

## 1. Startup Verification ✅

| Check | Status | Evidence |
|-------|--------|----------|
| `python -m app.runtime` starts cleanly | PASS | `scripts/e2e_smoke.py --start-server` |
| Documented env vars work (PORT, DB_PATH, PROFILE, LOG_LEVEL) | PASS | Manual test + smoke script |
| Health endpoint returns READY | PASS | `/api/v1/health` → `"status":"READY"` |
| Profile validation rejects unsafe combos | PASS | `config.py` ConfigError on production+SQLite |
| Embedded coordinator starts (default) | PASS | Health shows workers + coordinator stats |
| Clean SIGTERM shutdown | PASS | `test_runtime_sigterm_shutdown_is_clean` + smoke script |

---

## 2. Readiness Verification ✅

| Check | Status | Evidence |
|-------|--------|----------|
| `/api/v1/health` open (no auth) | PASS | Returns 200 with status |
| `/api/v1/status` requires auth in non-local profiles | PASS | Stage 10 RBAC |
| Worker registry exposed | PASS | `code-bud`, `repo-code-bud` listed |
| Configuration snapshot in health | PASS | `config_ok`, `profile`, `auth` fields |

---

## 3. Submit Task → Task ID ✅

| Check | Status | Evidence |
|-------|--------|----------|
| `POST /api/v1/missions` returns 200 + mission_id | PASS | Smoke test + `test_api.py` |
| Response includes `status: QUEUED` immediately | PASS | Non-blocking enqueue |
| Idempotency-Key header + body field | PASS | Replay returns `"replayed": true` |
| Dry-run returns PASS with context | PASS | Manual + smoke test |
| Unknown capability → BLOCKED (not 500) | PASS | Smoke test example 13 |
| Payload governance enforced (413) | PASS | `check_payload_governance` |
| Rate limit on submit (429 + headers) | PASS | `YODAW_MISSIONS_PER_MINUTE` bucket |

---

## 4. Poll Status ✅

| Check | Status | Evidence |
|-------|--------|----------|
| `GET /api/v1/missions/{id}` works | PASS | Smoke test polling loop |
| Status transitions observed: QUEUED → RUNNING → PASS | PASS | Smoke test output |
| Worker field populated | PASS | `worker: "code-bud"` |
| Heartbeat timestamps update | PASS | `heartbeat_at` in responses |
| Cross-tenant isolation (404 not 403) | PASS | `_can_read_mission` logic |

---

## 5. Retrieve Result / Evidence ✅

| Check | Status | Evidence |
|-------|--------|----------|
| `/api/v1/missions/{id}/evidence` returns structured log | PASS | 11 items for code worker |
| Evidence includes git cmds, test runs, diffs | PASS | Examples doc |
| `/api/v1/missions/{id}/events` returns internal events | PASS | 6 events for code worker |
| Terminal result includes `result` object | PASS | `tests_passed`, `commit_sha`, etc. |
| Error classification: task vs provider vs blocked_external | PASS | `error_class` field |

---

## 6. Cancel / Retry Paths ✅

| Check | Status | Evidence |
|-------|--------|----------|
| Cancel QUEUED → CANCELLED (200) | PASS | Smoke test + `test_cancellation.py` |
| Cancel RUNNING → CANCELLING (200) | PASS | Cooperative checkpoint cancel |
| Cancel terminal → 409 | PASS | Smoke test |
| Cancel unknown → 404 | PASS | Smoke test |
| Retry PASS → 409 | PASS | Smoke test |
| Retry FAIL/BLOCKED/CANCELLED → new QUEUED | PASS | Smoke test |
| Retry budget bounded (max 3 attempts) | PASS | `MAX_RETRY_ATTEMPTS = 3` |
| Lineage preserved (`retried_from_id`, `attempt_lineage`) | PASS | `retry_product_mission` |

---

## 7. Clean Shutdown ✅

| Check | Status | Evidence |
|-------|--------|----------|
| SIGTERM stops claiming new missions | PASS | Coordinator `stop(drain=True)` |
| Inflight missions drain (worker finishes or cancels) | PASS | `test_runtime_sigterm_shutdown_is_clean` |
| Repo leases released | PASS | `RepoLeaseManager` cleanup |
| Outbox relay stops | PASS | Lifespan shutdown |
| Storage closed without corruption | PASS | SQLite WAL consistent |

---

## 8. External Caller Workflow ✅

| Check | Status | Evidence |
|-------|--------|----------|
| Base URL configurable without code changes | PASS | Smoke script `--base-url` |
| JSON-only client works (curl, Python urllib, httpx) | PASS | Examples + smoke client |
| No undocumented manual steps required | PASS | README + OPERATIONS.md + scripts |
| Auth modes: local-dev (open), shared-key, RBAC | PASS | `auth_mode()` resolution |
| Errors are understandable (400/401/403/404/409/413/422/429) | PASS | All observed in smoke test |

---

## 9. Regression ✅

| Suite | Passed | Skipped | Duration |
|-------|--------|---------|----------|
| Full macOS regression (`run_regression_mac.sh`) | **776** | 7 | 106s |
| `test_api.py` | 3 | 0 | 1.6s |
| `test_runtime_service.py` | 2 | 0 | 3.6s |
| `test_stage10_e2e.py` | 1 | 0 | 3.3s |
| `test_cancellation.py` | 4 | 0 | 2.4s |

---

## 10. Documentation ✅

| Artifact | Location |
|----------|----------|
| Minimal API usage guide | `docs/api_e2e_usage.md` |
| Example requests/responses | `docs/api_examples.md` |
| This checklist | `docs/api_graduation_checklist.md` |
| Smoke test script | `scripts/e2e_smoke.py` |

---

## BLOCKERS FOR API GRADUATION

**None found.** All lifecycle stages pass on a clean environment using only the public HTTP API.

---

## FINAL VERDICT

**API_GRADUATION_READY**

The YODAW API on current master (`d89e8b2`) is usable by an external client from a clean environment. The documented startup command works, required configuration is explicit, errors are actionable, no undocumented steps are required, the base URL can be changed without source modification, and a JSON-only client operates the full lifecycle successfully.

---