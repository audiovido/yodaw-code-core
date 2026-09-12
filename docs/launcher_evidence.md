# Launcher / Runtime — Validation Evidence (Worker A)

Branch: `yodaw/product-launcher` (base `cf6cf2a6`)
Date: 2026-09-13

Entrypoint: `./yodaw` (equivalently `python -m app.launcher`).
The launcher owns a single service, `runtime` (`python -m app.runtime`,
which embeds API + coordinator + watchdog + outbox relay), health-gates
every spawn on `GET /api/v1/health`, and respawns a dead runtime with
capped backoff.

Validation run below (fresh port 49682, temp DB and runtime dir;
`YODAW_RUNTIME_DIR=/tmp/yodaw-evidence/rt`).

## 1. Fresh start

```
$ ./yodaw start
YODAW: started (supervisor pid 86221, http://127.0.0.1:49682/api/v1/health)
```
Exit 0. Supervisor daemon double-forks into its own session; runtime
child 86222 booted and passed the readiness gate.

## 2. Double-start safety

```
$ ./yodaw start
YODAW: already running (supervisor pid 86221); see `yodaw status`
```
Exit 0, no second supervisor, no second runtime, exactly one process
on the port. The supervisor holds an exclusive flock on
`supervisor.pid`; `start` and the supervisor both refuse to run while
another process answers the health endpoint.

## 3. Health pass

```
$ ./yodaw health
{ "service": "YODAW", "status": "READY", "auth": "local-dev", ... }

$ ./yodaw status
YODAW runtime: UP (supervisor pid 86221)
service         pid  state      restarts  uptime
runtime       86222  running           0  2s
  health: .../api/v1/health -> READY (auth=local-dev, profile=local, backend=sqlite)
```
Both exit 0. `status --json` emits the same state machine-readably.

## 4. Stop

```
$ ./yodaw stop
YODAW: stopped
```
Exit 0. Runtime drained (SIGTERM), supervisor removed `supervisor.pid`
and `state.json` (verified: pidfile exists after stop -> NO).

## 5. Restart

```
$ ./yodaw restart
YODAW: stopped
YODAW: started (supervisor pid 86247, ...)
```
Supervisor pid changed 86221 -> 86247, runtime 86222 -> 86248, healthy,
single listener on the port.

## 6. Crash recovery

```
$ kill -9 86248          # runtime hard-killed
$ sleep 8; ./yodaw status
YODAW runtime: UP (supervisor pid 86247)
service         pid  state      restarts  uptime
runtime       86268  running           1  7s
  health: ... -> READY
```
Runtime respawned automatically (86248 -> 86268), `restarts` incremented
to 1, service back to `running`, health green. Supervisor singleton and
pidfile unchanged.

## 7. No stray processes

```
$ ./yodaw stop
$ ps aux | grep -E "app.runtime|app.launcher"   # (excluding a pre-existing
                                                #  dev instance, pid 61515)
no stray processes
$ lsof -iTCP:49682 -sTCP:LISTEN
port 49682 free (no listeners)
```
Runtime dir retains only `yodaw.log` (state/pidfiles removed on stop).

## 8. Tests (relevant suites)

```
$ python3.12 -m pytest tests/test_launcher.py tests/test_runtime_service.py tests/test_portable_python_runtime.py
collected 13 items
tests/test_launcher.py::test_fresh_start_health_status_and_stop PASSED
tests/test_launcher.py::test_double_start_is_safe PASSED
tests/test_launcher.py::test_health_command_and_json_status PASSED
tests/test_launcher.py::test_restart_replaces_processes PASSED
tests/test_launcher.py::test_crash_recovery_respawns_runtime PASSED
tests/test_launcher.py::test_stop_leaves_no_stray_processes PASSED
tests/test_launcher.py::test_start_refuses_foreign_service_on_port PASSED
tests/test_runtime_service.py::test_runtime_health_and_mission_roundtrip PASSED
tests/test_runtime_service.py::test_runtime_sigterm_shutdown_is_clean PASSED
tests/test_portable_python_runtime.py   (4/4 PASSED)
============================== 13 passed in 27.84s ==============================
```
Launcher suite alone (7 tests) passed three consecutive runs.