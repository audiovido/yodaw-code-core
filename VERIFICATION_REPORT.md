# YODAW Coder V1 — Independent Parallel Verification Report

**Worker**: independent senior verification (review/grok-final-verification)
**Repository**: https://github.com/audiovido/yodaw-code-core.git
**Starting audit SHA**: `1984b67069794fc0a25c215a5694c0ebd761eef3`
(latest commit on `origin/audit/9router-final-acceptance` at fork time)
**My branch**: `review/grok-final-verification`
**Primary branch**: untouched — re-verified `origin/audit/9router-final-acceptance`
still at `1984b67` after all work.

## Commits Created (6)

| SHA | Subject |
|---|---|
| `1ae92d5` | fix(cli): plan-only fallback must never report PASS |
| `e58d608` | fix(worker): no-repo code mission must not report success |
| `39f55d7` | test(api): exercise real repo execution in API happy-path tests |
| `2d0f450` | fix(cli): TASK_COMMANDS includes setup-9router/models; forward --timeout |
| `9b37554` | fix(bootstrap): export install-private DATA_DIR for 9Router |
| `4b64867` | test: regressions for FD safety, retry ceiling, stream close |

438 insertions, 71 deletions across 17 files. No merge, no force-push;
branch pushed to origin for the primary worker to cherry-pick from.

## Bugs Found

### 1. FALSE-PASS: plan-only fallback reported PASS (HIGH)
`app/cli/pipeline.py` — with no execution backend wired (`executor=None`),
`run_task` returned `success=True, status="PASS"`, exit 0. `yodaw run` and
the REPL both default `executor=None`, so planning success alone produced
PASS — no edit, no validation, no execution evidence.
Fixed: NOT_EXECUTED, success=False, task failed, retryable=True.
Regression test: `test_run_task_without_executor_is_not_a_pass`.

### 2. FALSE-PASS: no-repo code mission reported success (HIGH)
`app/workers/code_worker.py` — missing `repo_path` returned `success=True`
-> coordinator terminal PASS. Reachable via UI (capability `code`, no repo
configured). Nothing was edited, validated, or committed.
Fixed: success=False + `error.type=NoRepositoryTarget` -> coordinator FAIL.
Regression tests: `test_code_worker_no_repo_is_not_a_pass`,
`test_product_status_transition_code_mission_reports_not_executed`.

### 3. DATA_DIR drift on setup-9router re-run (MEDIUM)
`setup-9router` and `models` missing from `TASK_COMMANDS` in
`app/cli/main.py`, so the persisted product gateway key file was never
applied on those subcommands. On macOS (no /proc discovery) a re-run
failed to find the local daemon or diverged toward `~/.9router`.
Fixed: both added to `TASK_COMMANDS`.
Regression test: `test_setup_9router_reuses_persisted_key_not_home_dir`.

### 4. DATA_DIR drift in bootstrap wrapper (MEDIUM)
`scripts/bootstrap.py` never exported the install-private `var/9router`
DATA_DIR, so the install-managed 9Router daemon drifted to `~/.9router`
on re-run. Fixed: export `DATA_DIR` from `$SCRIPT_DIR/../var/9router`
when present and DATA_DIR unset.

### 5. Dead CLI flag: `run --timeout` parsed but never forwarded (LOW)
`app/cli/main.py` — `--timeout` accepted but never passed to `run_task(...)`.
Fixed: forwarded.

## DATA_DIR Findings

- `default_data_dir()` (`$DATA_DIR or ~/.9router`) is the CLI-matching
  fallback; product-specific dirs are passed explicitly via `--data-dir`
  and, after fix #4, via wrapper `DATA_DIR` export.
- `start_daemon` propagates `DATA_DIR` cleanly;
  `resolve_admin`/`_candidate_data_dirs` search explicit -> env ->
  /proc discovery -> default.
- No other silent-fallback path remains after fixes 3+4. Note: /proc
  discovery is Linux-only; on macOS explicit env/flag is the only
  reliable channel (now covered).

## False-PASS Findings

- All PASS emissions gated: repo worker requires non-empty edits
  (InvalidEditPlan), a test runner (NoTestsDetected), dirty git status
  (NoChange), and passes the secret scan before commit.
- Coordinator emits PASS only on `result.success=True`; evidence report
  `terminal` derives from success.
- `dry_run` intentionally records PASS (documented/observable).
- After fixes 1+2 no path emits PASS from planning alone.

## SQLite / FD Findings

- No deterministic leak: manual `connect()` sites use try/finally/close;
  transaction helpers exception-safe; check/repair close on all paths.
- No GC reliance; `with connect()` relies on CPython refcount rebind
  (verified empirically, no leak).
- Added `test_no_descriptor_leak_over_many_connections` (150 store
  cycles, fd delta < 25).

## Retry / Timeout Findings

- Provider retries bounded (max 3, backoff+jitter, per style); route
  fallback chain rotates routes with total attempts = max_retries+1.
  Verified by `test_total_attempts_bounded_by_max_retries` — no
  multiplicative amplification.
- Streams closed deterministically on parser error —
  `test_stream_response_closes_on_parser_raise`.
- CancelContext enforces `metadata.timeout_seconds`; repo worker honors
  it. Known gap (documented, not fixed): daemon-side missions have no
  default deadline unless the API metadata supplies `timeout_seconds`.
  Not fixed to avoid breaking legit long missions; recommend
  documenting/optional env `YODAW_MISSION_TIMEOUT_SECONDS`.

## Security Findings

- Loopback everywhere: YODAW 127.0.0.1:8844, 9Router daemon 127.0.0.1,
  `start_daemon --host 127.0.0.1`. No public bindings.
- Key files 0600/0700 enforced (`write_api_key_file`,
  `_harden_key_file_permissions`). No raw keys in TOML, logs, or stdout
  (redaction; `_provisioning_summary` pops key before logging).
- Subprocess list-argv only, no `shell=True` — no injection path.
- Evidence JSON `plan_parse_failure` raw_snippet could echo secrets
  (LOW, documented). `app/product/version.py` executes repo's own
  version.py (self-trust, documented).
- Gateway key lifecycle: reuse; rotate only on rejected credentials;
  never rotate for quota errors. Correct.

## Test Totals

- Targeted: 154 passed, 1 deselected (live-LLM test) across 7 files.
- Broader: full `tests/` -> 1175 passed, 7 skipped, 2 failed.
  Both failures env-dependent (no LLM provider; `[Errno 61] Connection
  refused`), failing identically at baseline — not regressions.
- One transient full-suite run showed 494 errors (DB contamination);
  a clean re-run produced the numbers above. Environmental, not code.

## Tests Added

- `test_run_task_without_executor_is_not_a_pass` (cli_shell)
- `test_code_worker_no_repo_is_not_a_pass` (code_worker)
- `test_product_status_transition_code_mission_reports_not_executed`
- `test_setup_9router_reuses_persisted_key_not_home_dir` (ninerouter)
- `test_no_descriptor_leak_over_many_connections` (persistence)
- `test_total_attempts_bounded_by_max_retries` (llm_failover)
- `test_stream_response_closes_on_parser_raise` (streaming)
- 5 API/acceptance tests converted from false-PASS to real-repo execution
- `make_smoke_repo` / `smoke_mission_payload` helpers in tests/helpers.py

## Cherry-Pick Recommendations

| Commit | Recommend | Note |
|---|---|---|
| `1ae92d5` | YES | closes false-PASS; low risk |
| `e58d608` | YES | closes false-PASS; bare "code" missions now FAIL — verify repo-less flows expect honest FAIL |
| `39f55d7` | YES | test infra only; pairs with `e58d608` |
| `2d0f450` | YES | DATA_DIR/key-file reuse + dead flag fix |
| `9b37554` | YES | wrapper DATA_DIR export |
| `4b64867` | YES | regression tests only |

All six can be cherry-picked in order onto the audit branch after review.

## Independent Verdict

**FIXES_RECOMMENDED**

Two HIGH false-PASS defects confirmed and fixed, two MEDIUM DATA_DIR
drift defects fixed, one LOW dead-flag fixed, regression coverage added.
No BLOCKING defect remains in the reviewed scope.