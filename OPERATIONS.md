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
- **Known limits (Stage 9):** SQLite single-writer storage —
  Postgres behind the existing store interfaces is the intended
  next step for high concurrency; single shared admin key (no
  per-admin identities); no rate limiting beyond concurrency
  quotas.

## Test map

| Area | Tests |
|---|---|
| Identity lifecycle, revocation | `tests/test_tenancy.py` |
| Quotas (submission + race-free claim) | `tests/test_tenancy.py` |
| Priority/FIFO ordering | `tests/test_tenancy.py` |
| Audit immutability + redaction | `tests/test_tenancy.py` |
| Outbox exactly-once + crash window | `tests/test_outbox_relay.py` |
| Multi-tenant runtime E2E | `tests/test_stage9_e2e.py` |
| Stage 7/8 regression | all prior suites |
