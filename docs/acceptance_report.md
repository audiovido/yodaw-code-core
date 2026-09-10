# YODAW Release Acceptance Report (final-coder-release-v2)

Base: `yodaw/final-coder-release-v2` (from `yodaw/final-coder-release@2fac2bd`
+ Worker N `eef54db` + Worker O `14af881`)
Suite: `python3.12 -m pytest tests/acceptance -q`
Result: **23 passed, 0 failed** on the final release line.

## Scope

| Area | File | Tests |
|---|---|---|
| CLI | `tests/acceptance/test_cli_acceptance.py` | 6 passed |
| Mission API | `tests/acceptance/test_mission_api_acceptance.py` | 7 passed |
| Failure classification | `tests/acceptance/test_failure_acceptance.py` | 5 passed |
| Router | `tests/acceptance/test_router_acceptance.py` | 3 passed |
| Computer | `tests/acceptance/test_computer_acceptance.py` | 2 passed |

## Bugs found and fixed on this line

### BUG-1 (fixed in base): `CodeWorker` shelled out to bare `python`
- Worker O observed all 4 failures tracing to `FileNotFoundError: 'python'`
  on its older base. The final release line already runs code missions under
  `sys.executable`; this V2 line additionally centralizes resolution in
  `app/workers/python_runtime.py` (`sys.executable` → `YODAW_PYTHON` →
  `python3` → `python`) with `tests/test_portable_python_runtime.py`.
- Failing-then-passing tests: `test_code_mission_passes_with_evidence`,
  `test_router_dispatches_code_to_code_bud`,
  `test_code_worker_leaves_filesystem_evidence`,
  `test_code_worker_reports_workspace_and_commit`.

### BUG-2 (fixed in V2): `/api/v1/workers` leaked worker health shape
- Full-suite ordering appended test workers whose `health()` returned only
  `{"status": "READY"}`; the endpoint returned that raw shape, so acceptance
  tests reading `w["capabilities"]` raised `KeyError`.
- Fix: `app/workers/registry.py::status()` normalizes every entry to
  `{name, status, capabilities}` without touching routing or dispatch.
- Locked by `tests/test_workers_registry_shape.py`.
- Failing-then-passing tests: `test_workers_advertise_code_capability`,
  `test_router_registry_lists_supported_capabilities`.

## Worker O history (for context, not current state)

On Worker O's own older base (`c6e7d82`), the suite reported
**19 passed, 4 failed**, all 4 tracing to BUG-1 above.

## Scope

| Area | File | Tests |
|---|---|---|
| CLI | `tests/acceptance/test_cli_acceptance.py` | 6 passed |
| Mission API | `tests/acceptance/test_mission_api_acceptance.py` | 6 passed, 1 failed (product bug) |
| Failure classification | `tests/acceptance/test_failure_acceptance.py` | 5 passed |
| Router | `tests/acceptance/test_router_acceptance.py` | 2 passed, 1 failed (product bug) |
| Computer | `tests/acceptance/test_computer_acceptance.py` | 0 passed, 2 failed (product bug) |

## Bugs found

### BUG-1: `CodeWorker` shells out to bare `python`, which does not exist here — every `code` mission FAILs

- Failing tests: `test_code_mission_passes_with_evidence`, `test_router_dispatches_code_to_code_bud`, `test_code_worker_leaves_filesystem_evidence`, `test_code_worker_reports_workspace_and_commit`.
- Exact failure: mission reaches terminal state `FAIL` (worker `code-bud`) with `result.error = {"type": "FileNotFoundError", "message": "[Errno 2] No such file or directory: 'python'"}`.
- Location (not patched, per Worker O rules): `app/workers/code_worker.py` runs `["python", "-m", "pytest", "-q"]`. This environment provides `python3` / `python3.12` but no `python` on PATH (`which python` → not found).
- The same failure exists on the pre-existing baseline: `tests/test_api.py::test_code_mission` fails identically on this base without any Worker O changes.
- Reproducing tests are kept as-is; app code was intentionally left untouched.

## PENDING_WORKER_N

The following capabilities were probed and are **not present on this base**; no speculative tests were added for them:

- `PENDING_WORKER_N`: supervisor capability — no `supervisor` worker, capability, or endpoint exists in `app/` (only `code-bud`/`code` and `repo-code-bud`/`repo-code` are registered).
- `PENDING_WORKER_N`: intelligent/task router beyond capability dispatch — routing on this base is exact capability-name matching (`WorkerRegistry.find`); no router worker or routing policy endpoint exists.
- `PENDING_WORKER_N`: native computer-control worker — no `computer` capability or worker exists; computer coverage here is limited to `code-bud` filesystem evidence (workspace, git, pytest runs).
- `PENDING_WORKER_N`: native CLI binary — operations surface is `python -m app.operations` only; no packaged console entry point exists.

## Notes

- CLI `outbox-list` prints two consecutive JSON documents (stats + listing); acceptance tests parse both rather than assuming a single document. Test-scope handling only; no app change.
- CLI tests pass `--db` before the subcommand (argparse global position) and run the operations module as a subprocess with no `app` imports.
