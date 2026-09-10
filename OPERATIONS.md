# YODAW Code Core — Stage 9 Operations Guide

Multi-tenant runtime: client identities, quotas, queue fairness,
an exactly-once learning outbox, and the production service
entrypoint. Read `ARCHITECTURE.md` for Stage 7/8 internals.

## Running the service

```bash
cd ~/YODAW/yodaw-code-core
source .venv/bin/activate
python -m app.runtime            # API + coordinator + watchdog + outbox relay
```

Configuration (environment):

| Variable | Default | Purpose |
|---|---|---|
| `YODAW_DB_PATH` | `data/yodaw.db` | SQLite database path |
| `YODAW_PORT` | `8844` | HTTP port |
| `YODAW_HOST` | `127.0.0.1` | Bind address |
| `YODAW_API_KEY` | *(unset)* | Shared admin key; unset = local-dev open mode |
| `YODAW_MAX_CONCURRENT_MISSIONS` | `2` | Global executor bound |
| `YODAW_HEARTBEAT_SECONDS` | `30` | Heartbeat cadence |
| `YODAW_STALE_AFTER_SECONDS` | `4x heartbeat` | Watchdog staleness cutoff |
| `YODAW_LLM_TIMEOUT_SECONDS` | `1200` | Provider request budget |
| `YODAW_PROVIDER_MAX_RETRIES` | `2` | Transient provider retries |
| `YODAW_PROVIDER_BACKOFF_SECONDS` | `1.0` | Backoff base |
| `YODAW_ENABLE_GITHUB` | `true` | Reuse search toggle |
| `YODAW_EMBED_COORDINATOR` | `1` | Set `0` to run split-process coordinators |
| `YODAW_LOG_LEVEL` | `INFO` | Logging verbosity |

## Multi-tenancy (Stage 9)

### Client identities

Client API keys are hashes (SHA-256); the plaintext `yodak_...`
key is returned **exactly once** at creation and never stored.
Keys authenticate as a client identity carrying:

- `priority` — queue class, 1 (highest) … 9 (lowest). The queue
  claims by priority, then FIFO within a class.
- `max_concurrent_missions` — per-client execution quota.
  Queued missions always wait; the quota bounds simultaneous
  executions only, so a saturated client serializes instead of
  starving.

Admin surface (shared key or local-dev only — client keys can
never manage clients or read the audit trail):

```
POST   /api/v1/clients                    create (returns the key once)
GET    /api/v1/clients                    list (no key material)
POST   /api/v1/clients/{name}/priority    set queue priority
POST   /api/v1/clients/{name}/disable     revoke (key returns 401)
POST   /api/v1/clients/{name}/enable      restore
GET    /api/v1/audit?client_id=&mission_id=&action=&limit=   compliance view
```

Authentication resolution per request:

1. registered client key (hashed lookup) → identity with
   priority/quotas;
2. else the shared `YODAW_API_KEY` (Stage 8 contract, no
   identity);
3. else, when no shared key is configured, local-dev open mode.

A configured shared key + unknown/mismatched key is `401`.
Quota admission at submission time is `429`; the authoritative
race-free enforcement happens inside the claim transaction.

### Audit trail

Append-only `audit_events` table: `mission.created`,
`mission.rejected` (no worker / quota), `mission.cancel_requested`,
`clients.created/disabled/enabled/priority_set`. Payloads pass
recursive secret redaction (`api_key`, `authorization`, `token`,
`password`, …); rows are never updated or deleted.

### Exactly-once learning outbox

Mission completion writes a `learning.record` message into the
durable `mission_outbox` (same DB, same transaction domain as the
mission). A relay thread drains it with an eager in-process pass
for zero added latency. Delivery uses a deterministic record id
derived from the mission id with upsert semantics: replays after a
crash rewrite the identical row, so the observable effect is
exactly-once. Failed deliveries are recorded with `last_error`,
retried, and never dropped. `GET /api/v1/runtime/status` exposes
outbox counters.

### Queue fairness

Claim order: `priority ASC, created_at ASC` (index
`idx_missions_queue_order`). Quota-blocked candidates are skipped
without blocking lower-priority work; per-repo exclusion leases
(Stage 8) still serialize same-repo missions across clients.

## Production deployment

- **Process model:** `python -m app.runtime` is the single
  entrypoint (API + coordinator + watchdog + relay). For
  scale-out, run one API process and N coordinator processes with
  `YODAW_EMBED_COORDINATOR=0`; claims and quotas are
  transaction-atomic across processes.
- **Shutdown:** SIGTERM/SIGINT stop claiming, drain inflight
  missions, stop the relay, release leases, close storage.
- **Backups:** the database is a single SQLite file (WAL); snapshot
  with the usual SQLite online-backup procedure.
- **Secrets:** client keys are stored only as hashes; audit and
  evidence are redacted at the boundary. Never log the shared key.
## Production deployment (Stage 10)

- **Deployment profiles:** `YODAW_PROFILE` selects
  `local` (default: SQLite, open access, embedded coordinator,
  rate limiting off), `single-node` (auth required, rate limiting
  on), `multi-process` (split API + N coordinators; Postgres
  recommended, SQLite ceiling warned), `production` (Postgres
  required, auth required, shared-key-only refused unless
  `YODAW_SHARED_KEY_POLICY=warn`). Unsafe combinations fail fast
  at startup.
- **Database:** SQLite via `YODAW_DB_PATH` (default) or Postgres
  via `YODAW_DATABASE_URL` (psycopg 3). Postgres claims use
  `FOR UPDATE SKIP LOCKED`; quotas and audit chain semantics are
  identical across backends.
- **RBAC:** admin identities (`yodad_...` keys, hashed at rest)
  with roles `superadmin`, `operator`, `auditor`. Clients
  (`yodak_...`) are isolation-scoped to their own missions. The
  Stage 8 `YODAW_API_KEY` shared key remains as a virtual
  superadmin migration path and is deprecated: create real admin
  identities (`POST /api/v1/admins`) and stop distributing the
  shared key.
- **Governance:** per-client token-bucket rate limits
  (`YODAW_RATE_LIMIT_RPM`, `YODAW_RATE_LIMIT_BURST`,
  `YODAW_MISSIONS_PER_MINUTE`; 0 disables), persistence-backed so
  they hold across processes. Payload limits (goal length,
  metadata size) reject before any processing. All rejections are
  audited.
- **Audit integrity:** every event links
  `event_hash = SHA-256(prev_hash || canonical_payload)`.
  `GET /api/v1/audit/verify` or `python -m app.operations
  audit-verify` reports the first broken seq (exit code 2 on
  tampering). This is tamper-evident integrity for operational
  monitoring, not cryptographic non-repudiation.
- **Retention:** `python -m app.operations audit-prune
  --keep-days N [--archive path.jsonl] [--yes]` prunes old events
  with a verifiable chain-anchor boundary; the surviving chain
  still verifies.
- **Outbox operations:** generalized message kinds with typed
  schemas, backoff retries, and dead-letter after 5 attempts.
  Inspect with `outbox-list [--dead]`, requeue with
  `outbox-requeue [--id N]`, or the `/api/v1/outbox*` endpoints.
- **Backup:** `python -m app.operations backup DEST` performs a
  consistent SQLite online backup; on Postgres use
  `pg_dump`/WAL archiving.
- **Status:** `GET /api/v1/runtime/status` (safe, non-secret) or
  `python -m app.operations status`.
- **Shutdown:** SIGTERM/SIGINT stop claiming, drain inflight
  missions, stop the relay, release leases, close storage.
- **Scale ceiling:** SQLite is single-node (single writer);
  correctness holds under multi-process load but throughput
  serializes. Postgres is the scale-out path.

## Test map

| Area | Tests |
|---|---|
| Storage adapter contracts | `tests/test_storage_contracts.py` |
| Postgres adapter (hermetic + integration) | `tests/test_pg_contract.py` |
| Identity lifecycle, revocation, quotas | `tests/test_tenancy.py` |
| RBAC matrix, isolation, rotation | `tests/test_rbac.py` |
| Rate limits, governance, profiles | `tests/test_governance_and_profiles.py` |
| Audit chain, prune/archive, tamper detection | `tests/test_storage_contracts.py` |
| Generalized outbox (dead letter, unknown kinds) | `tests/test_outbox_generalized.py` |
| Outbox exactly-once + crash window | `tests/test_outbox_relay.py` |
| Scale-out (two coordinators, one DB) | `tests/test_scaleout.py` |
| Security/failure audit checklist | `tests/test_stage10_security_audit.py` |
| Stage 10 runtime E2E | `tests/test_stage10_e2e.py` |
| Multi-tenant runtime E2E (Stage 9) | `tests/test_stage9_e2e.py` |
| Stage 7/8 regression | all prior suites |
