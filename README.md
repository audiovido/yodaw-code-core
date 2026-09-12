# YODAW Code Core

## Launcher (one command)

`./yodaw` (or `python -m app.launcher`) is the product entrypoint: it
starts, supervises, and stops the whole runtime — API + embedded
coordinator + watchdog + outbox relay — as one unit. No shell
one-liners, no manual process management.

```bash
./yodaw start          # start the runtime (idempotent)
./yodaw status         # supervisor + service state + live health
./yodaw stop           # graceful shutdown (drain, SIGKILL fallback)
./yodaw restart        # stop, then start
./yodaw logs [-n 50] [-f]   # supervisor + runtime logs
./yodaw health         # print GET /api/v1/health
```

Behavior:

- **Single process set.** `start` spawns a detached supervisor that
  owns the `runtime` service (`python -m app.runtime`). Backend order
  is preserved: the runtime embeds API, coordinator, watchdog, and
  outbox relay, and every spawn passes the `/api/v1/health` readiness
  gate before it is declared running.
- **No duplicates.** The supervisor holds an exclusive flock on its
  pidfile, so a second `start` is a safe no-op. Both `start` and the
  supervisor refuse to run while another process already answers
  `YODAW_HOST:YODAW_PORT`.
- **Crash recovery.** If the runtime dies, the supervisor respawns it
  with capped backoff and records `restarts` / `last_error` in
  `status`.
- **Graceful shutdown.** `stop` signals the supervisor, which drains
  the runtime (SIGTERM, then SIGKILL after the 25s drain window)
  and removes its pidfile; orphans are reaped from the state file.

State and logs live in `YODAW_RUNTIME_DIR` (default `<repo>/.yodaw`:
`supervisor.pid`, `state.json`, `yodaw.log`) — repo-relative and
env-overridable, never a hardcoded user path. The interpreter is
`YODAW_PYTHON`, else `<repo>/.venv/bin/python`, else `python3.12`,
else `python3`. `status --json` emits the same state as inline text
for automation.

## Production Entrypoint (canonical)

Low-level equivalent of `./yodaw start` (used by the launcher itself):

```bash
source .venv/bin/activate
python -m app.runtime
```

Runs API + embedded coordinator + watchdog + outbox relay on `YODAW_HOST:YODAW_PORT`.

Defaults: `127.0.0.1:8844`, SQLite `data/yodaw.db`, profile `local` (open access).

## Configuration

One canonical TOML config file; no manual environment editing.
First run: `yodaw config init`, then `yodaw config validate`.
Effective (non-secret) settings: `yodaw config show` / `--json`.

File locations (first match wins):

- `YODAW_CONFIG` (explicit)
- `~/Library/Application Support/yodaw/config.toml` (macOS)
- `${XDG_CONFIG_HOME:-~/.config}/yodaw/config.toml`
- `./yodaw.config.toml`

```toml
[llm]
provider = "auto"   # auto | ollama | openai | anthropic
mode     = "auto"   # auto | local | remote
model    = "auto"   # model id, or "auto" for the provider default
# base_url = "..."            # override the provider endpoint
# api_key_env = "OPENAI_API_KEY"

[server]
profile = "local"   # local | single-node | multi-process | production
port    = 8844

[logging]
level = "INFO"
```

Environment variables always win over the file (`YODAW_LLM_PROVIDER`,
`YODAW_LLM_MODE`, `YODAW_LLM_MODEL`, `YODAW_LLM_BASE_URL`,
`YODAW_LLM_API_KEY`, `YODAW_LLM_API_KEY_ENV`, `YODAW_PROFILE`,
`YODAW_HOST`, `YODAW_PORT`, `YODAW_LOG_LEVEL`). Keys live in
environment variables only — the file names the variable, and neither
the file nor logs ever contain the secret.

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
