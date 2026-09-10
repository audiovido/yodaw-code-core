# YODAW Code Core

Run locally:

    source .venv/bin/activate
    uvicorn app.main:app --host 127.0.0.1 --port 8844

Health:

    curl http://127.0.0.1:8844/api/v1/health

Mission:

    curl -X POST \
      http://127.0.0.1:8844/api/v1/missions \
      -H "Content-Type: application/json" \
      -d '{"goal":"Test YODAW Code Core","capability":"code"}'

API docs:

    http://127.0.0.1:8844/docs

## Developer testing

Requires Python 3.12 (`python3.12` on PATH).

Canonical regression command (macOS):

    ./scripts/run_regression_mac.sh

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
