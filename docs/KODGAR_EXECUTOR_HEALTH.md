# Kodgar Executor Health

Executors are the coding agents Kodgar can drive. Before this change the
registry only asked *"does the binary exist?"* — which is not health. A CLI
can be installed, authenticated, and still be unable to produce a single
token, and Kodgar would happily route real work into it.

This document describes the health model that replaced binary detection.

## The failure that motivated it

Measured on this machine (`2026-09-18`):

| Executor | Binary | Version | Real inference |
|---|---|---|---|
| `claude-code` | present (`~/.npm-global/bin/claude`) | 2.1.273 | **fails** — model routes unavailable |
| `grok-cli` | present (`~/.grok/bin/grok`) | 1.0.30 | **fails** — upstream `403 FreeTierError` |
| `codex` | absent | — | n/a |
| `kodgar-native` | in-process | — | works (local model) |

Binary-only detection reported three "available" executors while only one
could actually complete work.

### Claude root cause

`~/.claude/settings.json` points the Claude CLI at the local 9Router
gateway (`ANTHROPIC_BASE_URL=http://127.0.0.1:20128`). The CLI's built-in
aliases (`claude-opus-5[1m]`, `claude-sonnet-5`) do not exist in the
gateway catalog, so every default invocation died with
`model_not_found` / `api_error_status=404` **before any provider call**.

That mapping defect was repaired in place (a backup was written first:
`~/.claude/settings.json.bak-kodgar-health`). The CLI now resolves to a
route that genuinely exists in the gateway (`kr/claude-sonnet-4.5`). The
remaining failure is external and truthful: that route returns
`402 MONTHLY_REQUEST_COUNT` (quota exhausted) and the alternate families
return 403. So Claude is now *installed + authenticated + correctly
mapped*, and still **unhealthy for a quota reason Kodgar cannot repair**.

## Health semantics

`app/background/executors/health.py` produces a structured verdict per
executor:

```json
{
  "id": "claude-code",
  "installed": true,
  "authenticated": true,
  "model_available": false,
  "inference_ok": false,
  "healthy": false,
  "eligible": false,
  "latency_ms": 1658,
  "error_type": "quota_exhausted",
  "detail": "402 MONTHLY_REQUEST_COUNT ...",
  "checked_at": "2026-09-18T09:08:51Z",
  "cooldown_until": null,
  "circuit_open": false
}
```

The fields are deliberately separated:

| Field | Meaning |
|---|---|
| `installed` | the binary/entry point exists |
| `authenticated` | credentials are present and accepted |
| `model_available` | a model route actually resolves |
| `inference_ok` | a real, minimal generation completed |
| `healthy` | all of the above hold |
| `eligible` | healthy **and** the circuit is closed — the only state routing may use |

`healthy` and `eligible` are separate because health is a property of the
environment while eligibility is a routing decision that a recent failure
can revoke.

### Probes are real and bounded

Each probe performs a minimal real generation through the executor's own
CLI (never a `--help`):

- `claude-code`: `claude -p "Reply with exactly CLAUDE_OK"`
- `grok-cli`: `grok -p "Reply with exactly GROK_OK"` (headless)
- `codex`: availability + a bounded version/auth probe
- `kodgar-native`: in-process provider reachability

Every probe is wrapped in `_run_bounded(..., timeout=DEFAULT_PROBE_TIMEOUT)`
(45 s default). **No probe can hang a request**, and a probe that times out
becomes `timeout` / unhealthy rather than blocking the API.

### Cache and TTL

Probing four executors on every request would be absurd (and `GET
/api/v1/executors` is polled by the UI). Results are cached:

| Knob | Default | Env |
|---|---|---|
| TTL | 120 s | `KODGAR_HEALTH_TTL_SECONDS` |
| Probe timeout | 45 s | `KODGAR_HEALTH_PROBE_TIMEOUT` |
| Cooldown | 300 s | `KODGAR_HEALTH_COOLDOWN_SECONDS` |

Within the TTL, `GET /api/v1/executors` is a cache read. `?refresh=1`
forces a re-probe for operators.

## Circuit breaker

`ExecutorHealthService.record_failure(id, message, status)` classifies the
failure and, for **infrastructure-class** errors only, opens the breaker
for that executor:

```
record_failure -> classify_executor_error -> error_class_action
              -> _CircuitState.record_failure -> cooldown_until = now + 300s
```

While `cooldown_until` is in the future the executor is `eligible: false`,
so no new task can be routed into a known-broken executor.

Two details matter and were both learned from real failures:

1. **A successful probe does not erase live failure counts.** Health
   probing and execution failure are different signals. An earlier version
   cleared the counter on any healthy probe, which made the breaker
   impossible to open for the exact case that matters ("probe OK,
   execution broken"). Failures now clear only on a real execution
   success or after the cooldown expires.
2. **Recovery is automatic.** Once the cooldown elapses the executor is
   re-probed on the next TTL expiry, so a fixed provider heals without
   manual intervention.

## API

```http
GET /api/v1/executors            # cached health, keyed by executor id
GET /api/v1/executors?refresh=1  # force a probe
```

```json
{
  "executors": {
    "claude-code":   { "healthy": false, "eligible": false, "error_type": "quota_exhausted", ... },
    "grok-cli":      { "healthy": false, "eligible": false, "error_type": "upstream_forbidden", ... },
    "codex":         { "installed": false, "healthy": false, "eligible": false, "error_type": "not_installed", ... },
    "kodgar-native": { "healthy": true,  "eligible": true,  "error_type": null, ... }
  }
}
```

The response is a **map keyed by executor id**, which is what the Terminal
UI consumes (`Object.entries(executors)`). The reasons are never hidden:
the sidebar renders unhealthy executors in red with `error_type` and the
probe `detail` as the tooltip.

## Tests

`tests/test_executor_health.py` (24 cases) covers the health model
directly, including: installed-but-invalid-model ⇒ unhealthy, Claude
`model_not_found` ⇒ fallback, grok 403 ⇒ fallback, absent Codex ignored,
cache TTL, circuit open/recovery, and "no fake completion".
