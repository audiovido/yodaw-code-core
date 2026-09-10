# YODAW Canonical Product API — `/api/v1`

Worker I product surface. One mission in, one terminal result out.

## Mission lifecycle

`QUEUED → OBSERVING → PLANNING → EXECUTING → VERIFYING → RECOVERING → PASS`

Terminal states: `PASS`, `FAIL`, `BLOCKED_EXTERNAL`, `CANCELLED`.
Provider and network faults end as `BLOCKED_EXTERNAL`, never as task
`FAIL`. `BLOCKED` (legacy unknown-capability shape) is exposed as `FAIL`.

## Endpoints

### `POST /api/v1/missions` — submit one mission

Body:

```json
{
  "goal": "add retry to the sync client",
  "repo_path": "/path/to/repo",
  "repo_ref": "owner/repo@sha",
  "capability": "repo-code",
  "constraints": {"max_files": 5},
  "model": {"name": "preferred-model"},
  "provider": {"name": "preferred-provider"},
  "dry_run": false,
  "idempotency_key": "unique-key-per-mission"
}
```

Only `goal` is required. The `Idempotency-Key` header is accepted as an
alias for `idempotency_key`. Replaying a key returns the original
mission with `replayed: true` instead of executing twice.

Response:

```json
{
  "mission_id": "m_abc123",
  "id": "m_abc123",
  "status": "QUEUED",
  "created_at": "2026-09-10T10:00:00+00:00",
  "updated_at": "2026-09-10T10:00:00+00:00",
  "attempt": 1,
  "attempts": 1,
  "error_class": null,
  "result": {},
  "replayed": false,
  "links": {
    "self": "/api/v1/missions/m_abc123",
    "evidence": "/api/v1/missions/m_abc123/evidence",
    "cancel": "/api/v1/missions/m_abc123/cancel",
    "retry": "/api/v1/missions/m_abc123/retry"
  }
}
```

Aliases: `POST /api/v1/product/missions` and
`POST /api/v1/product-missions` accept the same body.

### `GET /api/v1/missions/{mission_id}` — mission status

Returns the stored mission. Clients only see their own missions
(unknown ids and other tenants' ids both return `404`).

### `GET /api/v1/missions/{mission_id}/evidence` — evidence

```json
{"mission_id": "m_abc123", "evidence": [{"type": "product_context", "...": "..."}]}
```

### `POST /api/v1/missions/{mission_id}/cancel` — cancel

Race-safe. Returns `200` with `CANCELLED`/`CANCELLING`, or `409` when
the mission already finished. Unknown ids return `404`.

### `POST /api/v1/missions/{mission_id}/retry` — retry

Only terminal `FAIL`, `BLOCKED_EXTERNAL`, `BLOCKED`, and `CANCELLED`
missions retry. The new attempt keeps prior evidence, records
`retried_from_id` lineage, and appends to the parent's
`attempt_lineage`. Bounded to 3 product attempts; exhausted budgets and
active missions return `409`.

```json
{
  "mission_id": "m_new",
  "retried_from": "m_old",
  "status": "QUEUED",
  "links": {"self": "...", "evidence": "..."}
}
```

### `GET /api/v1/status` — service status

Returns service readiness, mission counts, and worker health.

### `GET /api/v1/capabilities` — capabilities

Returns worker capabilities plus the canonical `mission_statuses` list.

## Operator page

Minimal static page at `GET /product` (no framework): goal input,
submit, mission id/status, polling, evidence log, final result, and a
copy-result button.

## Backward compatibility

Legacy `MissionCreate` payloads that nest `repo_path`, `dry_run`, or
`idempotency_key` under `metadata` keep working; the canonical route
lifts them to first-class fields. Older internal routes are unchanged.
