# YODAW First Run Guide

## What Happens on First Run

When you start YODAW for the first time, several initialization steps occur:

1. **Database Creation**: YODAW creates its SQLite database at `~/.yodaw/var/yodaw.db` (or in the `var` directory of your custom installation).

2. **Default Configuration**: YODAW starts with the `local` profile, which provides:
   - Open access (no authentication required)
   - Local development settings
   - Embedded coordinator enabled
   - Rate limiting disabled

3. **Service Initialization**: The YODAW runtime starts these embedded services:
   - HTTP API server (FastAPI + Uvicorn)
   - Embedded coordinator (manages mission queue and worker heartbeats)
   - Watchdog (monitors worker health)
   - Outbox relay (handles outbound HTTP requests)

## Verifying First Run Success

After starting YODAW with `~/.yodaw/bin/yodaw`, you should see log output indicating the service is ready:

```
[INFO] YODAW runtime listening on 127.0.0.1:8844
```

You can verify the service is healthy with:

```bash
curl http://127.0.0.1:8844/api/v1/health
```

Expected response:
```json
{
  "service": "YODAW",
  "status": "READY",
  "auth": "local-dev",
  "profile": "local",
  "storage_backend": "sqlite",
  "repository_bound": [],
  "workers": {},
  "config_ok": true
}
```

## Submitting Your First Mission

You can test YODAW by submitting a simple mission. Here's an example using the Python basic fixture:

```bash
# Submit a mission to verify the Python basic fixture
curl -X POST http://127.0.0.1:8844/api/v1/missions \
  -H "Content-Type: application/json" \
  -d '{
    "goal": "Verify Python basic fixture works",
    "capability": "repo-code",
    "repo_path": "./eval/fixtures/python_basic"
  }'
```

This should return a mission ID immediately:
```json
{"id": "m_abc123", "status": "QUEUED"}
```

## Checking Mission Status

Poll for the mission result using the mission ID:

```bash
MISSION_ID=m_abc123
curl "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID"
```

Continue polling until the status is `PASS`, `FAIL`, or another terminal state.

## Getting Mission Evidence

Once the mission completes, you can retrieve the evidence:

```bash
curl "http://127.0.0.1:8844/api/v1/missions/$MISSION_ID/evidence"
```

## Stopping YODAW

To stop YODAW cleanly, press `Ctrl+C` in the terminal where it's running, or use:

```bash
~/.yodaw/bin/yodaw-uninstall
```

Note: The uninstall script only removes the installation; it doesn't stop a running YODAW instance. To stop a running instance, use `Ctrl+C` or send SIGTERM to the process.

## Data Persistence

YODAW stores all persistent data in the `var` directory of your installation:
- SQLite database: `var/yodaw.db`
- Logs: Written to stdout/stderr (can be redirected)
- Temporary files: Managed by the operating system

Your mission history, configuration, and other data will persist between runs as long as the `var` directory is preserved.