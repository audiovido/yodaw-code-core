# Kodgar Executor Acceptance (real evidence)

Every result below comes from a live run against the Task API on
`127.0.0.1:8844`, verified on disk. Nothing was inferred from planner
output, an exit code, or binary detection alone.

## Environment

| Item | Value |
|---|---|
| Kodgar core | `/Users/arminshokri/Kodgar` @ `feature/kodgar-background-v1` (`9466002`) |
| Clean worker source | `/Users/Shared/kodgar-worker-source` (Kodgar clone, synced to `9466002`) |
| Frontend | `/Users/Shared/kodgar-terminal-ui-v1` @ `1c46b3b` |
| Worktree root | `/Users/Shared/kodgar-worktrees` (outside every user working copy) |
| Planner | Grok planner chain, served by **9router** |
| Local coding model | `qwen2.5-coder:7b` (via `kodgar-native`) |

## Executor reality check (live API)

```
$ curl -s http://127.0.0.1:8844/api/v1/executors
claude-code     installed=True   healthy=False  eligible=False  err=quota_exhausted
codex           installed=False  healthy=False  eligible=False  err=not_installed
grok-cli        installed=True   healthy=False  eligible=False  err=upstream_forbidden
kodgar-native   installed=True   healthy=True   eligible=True   err=None

eligible: ['kodgar-native']
```

- **Claude**: config defect repaired (model alias mapped to a route the
  gateway actually serves; backup kept at `~/.claude/settings.json.bak-kodgar-health`).
  Inference is now blocked only by the provider: `402 MONTHLY_REQUEST_COUNT`.
  Reported truthfully as unhealthy.
- **Grok**: logged in, but both the default model and `--model 9router`
  fail with `403 FreeTierError: OpenCode's free tier can only be used from
  within OpenCode`. A headless probe cannot succeed — genuinely unhealthy.
- **Codex**: not installed on `PATH`. Ignored, never assumed usable.
- **Native**: healthy and the only eligible executor; it completed every
  acceptance task below.

## TEST C — native real edit task

Task `t_83a95b2ccb1b`, repo `/tmp/kodgar_accept_repo`:

```
state=COMPLETED  executor=kodgar-native  planner_backend=9router
branch=yodaw/task-10327-1-1789730760-d36f9045  files_changed=1
commit=b8b03f7f5787a677c2af8551abe892c30ecf5a5d
```

Disk evidence:

```
$ git show yodaw/task-10327-1-...:ACCEPTANCE_NATIVE.txt
ACCEPTANCE_NATIVE_OK
$ git -C /tmp/kodgar_accept_repo log --oneline -1
1ee955c baseline          # user's branch untouched, tree clean
```

Verifier checks (from the task record): `worktree PASS`,
`files_changed PASS`, `expected_files PASS`, `exact_content PASS`,
`scope PASS`, `tests PASS` (real pytest run), `commit PASS`.

## TEST D — health gate redirects an unhealthy planner choice

Task `t_f5d3b363c227`, submitted with `preferences.executor = "claude-code"`:

```
executor.selected  executor=kodgar-native  chain=["kodgar-native"]
reason="user override claude-code rejected by health (unavailable;
        error_type=quota_exhausted; ⚠ Claude Sonnet 4 was retired on
        June 15, 2026 ...); fell back to kodgar-native"
```

```
state=COMPLETED  commit=91f12f1c6fde09bdb1432c5cac8a2393170cc950
$ git show yodaw/task-10327-3-...:ACCEPTANCE_GATE.txt
ACCEPTANCE_GATE_OK
```

Kodgar never entered the broken executor, recorded *why*, and completed
the task on the healthy one.

## Per-attempt evidence

Task `t_4fdbb6eb9ecd` shows the recorded attempt history:

```json
"executor_attempts": [{
  "executor": "kodgar-native", "attempt": 1,
  "started_at": "...11:30:09.728606Z", "finished_at": "...11:30:10.105754Z",
  "duration_seconds": 0.13, "exit_code": 0, "error_type": null,
  "action": "SUCCESS", "worktree": ".../repo_14407-2-...", "changed_files": 1
}]
```

Its verifier report:

```
verify_status: PASS      tests: {passed: 1, failed: 0}   build: SKIPPED
  worktree        PASS   isolated worktree on yodaw/task-14407-1-...
  files_changed   PASS   1 changed file(s)
  expected_files  PASS   all 1 planned file(s) exist and changed
  exact_content   PASS   1 file(s) match the required content exactly
  scope           PASS   every change is inside the declared scope
  syntax          SKIPPED (no python/javascript files changed)
  tests           PASS   python -B -m pytest -q -p no:cacheprovider exited 0
  build           SKIPPED (the plan declared no build step)
  acceptance      PASS
  commit          PASS   commit 9892ad68... contains 1 file(s)
```

## Phase J — real frontend task

Goal: add the missing `executor.fallback` case to the Terminal UI's live
stream switch (so the new fallback behaviour is visible to users).

Task `t_fef3805870b3`, repo `/Users/Shared/kodgar-terminal-ui-v1`:

```
state=COMPLETED  executor=kodgar-native  files_changed=1
branch=yodaw/task-14407-5-1789731323-7ec762d1
commit=39d8668f9a0fcadffb54e5a9ab24a93dc878510f
checks: tests PASS "npm run build exited 0"   build PASS   commit PASS
```

The commit diff is exactly the intended change:

```diff
       case "executor.selected": push("kodgar",`Executor: ${executorLabel(data.executor)}`); break;
+      case "executor.fallback": push("warn", `Executor fallback: ${data.reason || data.error_type || "switched executor"}`); break;
```

The build ran in a **fresh worktree with no `node_modules`**, which is why
the verifier now installs JS dependencies first (see
`KODGAR_EXECUTOR_FALLBACK.md` §6).

### A real bug this test exposed (and fixed)

An earlier attempt at this same task (`t_81ef8babdbbf`) failed with
`FindTextMissing`: the planner had invented an edit anchor that does not
exist in the file. The engine classified that as `unknown` →
`RETRY_DIFFERENT_EXECUTOR`, so Kodgar abandoned a perfectly healthy
executor and the task died after one attempt.

That is a task/plan defect, not executor ill-health. Classification now
recognises `edit_not_applicable` and routes it to
`NON_RETRYABLE_TASK_ERROR`, so the task fails truthfully *without*
discarding a healthy executor or burning another executor's attempt.
Covered by `test_edit_anchor_mismatch_is_a_task_defect_not_executor_failure`.

## TEST H — API restart safety

The API process was killed and restarted mid-session. Afterwards:

```
counts: {'COMPLETED': 2, 'FAILED': 3}
t_f5d3b363c227 COMPLETED kodgar-native
t_83a95b2ccb1b COMPLETED kodgar-native
```

No task was left in `RUNNING`, history survived, and a fresh health probe
reported the same truthful verdicts (`claude-code` `quota_exhausted`,
`grok-cli` `upstream_forbidden`, `kodgar-native` eligible).

## Phase K — worker tasks from clean source

The specific failure Phase K asked to fix — tasks dying on `DirtyRepo`
*before* the executor ran — **is fixed**. Startup Builder
(`t_eeda86296109`), Marketing Agent (`t_441425494f5d`) and Business
Research (`t_275e9084ef94`) were submitted against
`/Users/Shared/kodgar-worker-source`. All three allocated an isolated
worktree and reached real execution on `kodgar-native` (two of them
reached `TESTING`, running the repository's own suite). No task failed on
`DirtyRepo`.

They did **not** reach `COMPLETED`, and the reason is not the executor
layer: their verification runs the *target repository's* test suite, and
that suite is red — see "Pre-existing failures" below. Verification
refused to commit on top of a failing suite, which is the intended
contract.

## Pre-existing failures in the target repository's suite

Because verification runs the target repo's real suite, the state of that
suite decides whether any task against that repo can complete. A full-suite
comparison was run to establish causality:

| Run | Result |
|---|---|
| Pristine baseline `5e0fbdc` (none of this work applied) | **11 failed**, 1263 passed, 7 skipped (9:24) |
| After this work | **4 failed**, 1296 passed, 7 skipped |

The four that still fail are a **strict subset of the baseline failures**,
so they are pre-existing and unrelated to the executor layer:

```
tests/test_ninerouter.py::test_mission_e2e_through_mock_9router
tests/test_product_ui.py::test_invalid_non_git_repository_fails_gracefully
tests/test_product_ui.py::test_real_acceptance_flow_submit_to_evidence
tests/test_tenancy.py::test_tenant_mission_carries_identity_priority_and_quota
```

The other seven baseline failures were repaired by this work
(`test_atomic_multi_edit_worker`, `test_bootstrap`, `test_resilience_audit`,
both `test_workers_safe_subprocess` cases, `test_workers_worktree_guard`,
`test_worktree_lifecycle`).

### Characterisation of the four remaining failures

All four are in the **mission runtime** (`app/runtime/*`), a subsystem this
change does not touch, and all four wait on a mission the runtime never
starts:

```
AssertionError: mission m_c3eb6b8f3151 did not reach a terminal state within 120.0s
AssertionError: assertion <MissionStatus.QUEUED> == 'RUNNING'
```

Measured facts:

- They **pass in isolation** (each alone, per-file, and in several file
groups: `test_tenancy.py` alone → 9 passed; `test_auth_and_events.py` +
failing test → 11 passed; `test_cancellation.py` +
`test_runtime_queue_and_claims.py` + failing test → 14 passed).
- They **fail only in a full-suite run**, and only ever with `QUEUED` /
not-terminal status.
- **Raising the deadlines did not fix them** — 10→60 s, 30→120 s,
60→180 s and 120→300 s all still failed. That measurement is why the
timeout inflation was **reverted** rather than shipped: the timeout is not
the invariant.
- The runtime's concurrency is capped (`YODAW_MAX_CONCURRENT_MISSIONS`,
default **2**) and the coordinator is a process-global singleton whose
`_inflight` state spans tests, so a mission left in flight by an earlier
test can leave no capacity to claim later missions. That mechanism fits
every observation (QUEUED status, isolation-only passing, timeout
immunity) but was not conclusively proven — the targeted group attempts
above did not reproduce it.

Per the acceptance rule, work stopped here rather than looping: this is a
true pre-existing blocker in a subsystem outside the executor repair, and
it is reported rather than papered over.

## Verifier reporting bug found and fixed

Both Phase K tasks reported `tests: {passed: 1295, failed: 0}` while
pytest **exited 1**. The count parser assumed pytest's summary order
(`"M passed, N failed"`), but pytest prints `"4 failed, 1295 passed"`, so
the `passed` match consumed the line and failures were silently reported
as zero. Counts are now extracted independently, errors are counted as
failures, and a regression test covers every observed ordering
(`test_test_count_parsing_handles_pytest_summary_order`).

## What could not be demonstrated on this machine

A **cross-executor completion fallback** (one executor fails mid-run, a
*different* one finishes the task) needs two genuinely healthy executors.
This machine has exactly one: Claude is quota-blocked upstream (402) and
Grok is restricted to OpenCode (403). Both are external account/provider
limits, not local defects. The machinery itself — health gate, chain
construction, attempt bound, classification, circuit breaker, persisted
attempt history — is exercised by `tests/test_executor_health.py`
(25 tests) and by the two live tasks above.
