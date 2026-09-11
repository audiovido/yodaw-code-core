# YODAW Code Core

## Production Entrypoint (canonical)

```bash
source .venv/bin/activate
python -m app.runtime
```

Runs API + embedded coordinator + watchdog + outbox relay on `YODAW_HOST:YODAW_PORT`.

Defaults: `127.0.0.1:8844`, SQLite `data/yodaw.db`, profile `local` (open access).

## Quick Health Check

```bash
curl http://127.0.0.1:8844/api/v1/health
# {"service":"YODAW","status":"READY","auth":"local-dev","profile":"local",...}
```

## Submit a Mission

```bash
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{"goal":"Test YODAW Code Core","capability":"code"}'
# Returns QUEUED immediately with mission_id
```

## Poll for Result

```bash
MISSION_ID=m_abc123
curl "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID"
# Poll until status in {PASS,FAIL,BLOCKED_EXTERNAL,CANCELLED}
```

## Get Evidence

```bash
curl "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID/evidence"
```

## Cancel / Retry

```bash
curl -X POST "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID/cancel" -d '{}'
curl -X POST "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID/retry" -d '{}'
```

## API Documentation

- Swagger UI: `http://127.0.0.1:8844/docs`
- Minimal usage guide: `docs/api_e2e_usage.md`
- Example requests/responses: `docs/api_examples.md`
- Graduation checklist: `docs/api_graduation_checklist.md`

## E2E Smoke Test (Worker E deliverable)

```bash
# Against existing server
python3.12 scripts/e2e_smoke.py --base-url http://127.0.0.1:8844

# Start server + run full lifecycle
python3.12 scripts/e2e_smoke.py --base-url http://127.0.0.1:8888 --start-server
```

## Developer Testing

Requires Python 3.12 (`python3.12` on PATH).

Canonical regression command (macOS):

```bash
./scripts/run_regression_mac.sh
```

The script creates a temporary `python` -> `python3.12` shim
because some tests and subprocesses invoke `python`, which does
not exist on stock macOS (only `python3`). The shim lives in a
temporary directory, is prepended to PATH for that run only, and
is removed on exit: no permanent shell or system changes.

macOS open-file limit: a low limit (e.g. `ulimit -n 256`) makes
the full suite fail incorrectly with `Too many open files` /
`sqlite3.OperationalError: unable to open database file`. The
script raises the limit to 4096 when permitted and prints the
before/after values. Extra args pass through to pytest
(e.g. `./scripts/run_regression_mac.sh -x -q`).

Do not interpret resource-exhaustion failures as code
regressions: on `Too many open files` or sqlite
`unable to open database file`, retry once with a clean
environment (`ulimit -n 4096`, Python 3.12) before investigating
code. The pytest exit code is authoritative.
