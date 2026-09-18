# Kodgar Executor Selection, Fallback, and Execution Source

Companion to `KODGAR_EXECUTOR_HEALTH.md`. That document defines *what
healthy means*; this one defines *what Kodgar does about it*.

## One rule

> A task must never fail merely because the executor the planner asked for
> is broken, while a healthy executor sits idle.

Everything below follows from that sentence.

## 1. Health-gated selection

The planner still chooses an executor, and its choice is still recorded —
but it is a *preference*, not a command. `ExecutorRegistry.select_chain()`
returns an ordered chain containing **only currently eligible executors**:

```python
FALLBACK_ORDER = ("claude-code", "codex", "grok-cli", "kodgar-native")

DEFAULT_BY_TASK_TYPE = {
    "fullstack_feature": "claude-code",
    "bugfix":            "codex",
    "research":          "grok-cli",
    "simple":            "kodgar-native",
    # ...
}
```

Decision order:

1. The planner's requested executor, **if eligible**.
2. Otherwise the capability default for the task type, if eligible.
3. Otherwise the first eligible entry of `FALLBACK_ORDER`.

`kodgar-native` is last in the order because it depends only on Kodgar's
own engine — it is the reliable backstop.

The rejection is not silent. The chain, the planner's reason, and the
health rejection all reach the event stream, and the task's
`executor_reason` records what happened:

```
executor.fallback  reason="health gate rejected claude-code: quota_exhausted"
executor.selected  executor="kodgar-native" attempt=1
```

When *nothing* is eligible the task ends `BLOCKED` with
`NoExecutorEligible` — an environment problem reported honestly, not a
code failure and not a fake success.

## 2. Bounded automatic fallback

Execution attempts are bounded by a single constant:

```python
MAX_EXECUTOR_ATTEMPTS = 3
```

The chain is expanded so that each entry appears twice. That gives a
transient failure **exactly one same-executor retry**, while fatal
failures are added to `seen_fatal` and never re-entered. The attempt bound
caps total work regardless of chain length, so no loop is possible — the
`for` loop can only ever visit three attempts.

Each attempt is a **fresh isolated worktree**; a failed attempt's worktree
is cleaned up before the next begins. Every attempt is appended to
`result.executor_attempts`:

```json
{
  "executor": "claude-code",
  "attempt": 1,
  "started_at": "...",
  "finished_at": "...",
  "duration_seconds": 12.4,
  "exit_code": 1,
  "error_type": "quota_exhausted",
  "action": "RETRY_DIFFERENT_EXECUTOR",
  "error": "402 MONTHLY_REQUEST_COUNT ...",
  "worktree": "/Users/Shared/kodgar-worktrees/repo_...",
  "changed_files": 0,
  "fallback_reason": "quota_exhausted -> retry with kodgar-native"
}
```

Because the history is persisted in the task record, the reason for every
executor switch survives an API restart and is visible in the UI.

## 3. Error classification

`classify_executor_error(message, status)` maps real CLI/provider evidence
to one of four actions:

| Action | Examples | Behaviour |
|---|---|---|
| `RETRY_SAME_EXECUTOR` | transient timeout, 503, provider overload | one same-executor retry |
| `RETRY_DIFFERENT_EXECUTOR` | `model_not_found`, auth failure, provider unavailable, unsupported headless mode | mark executor ineligible, take the next |
| `NON_RETRYABLE_TASK_ERROR` | invalid repo, impossible acceptance, security rejection | fail immediately, no retry |
| `VERIFICATION_FAILURE` | code produced but tests/build/acceptance failed | verification owns the verdict |

Classification is driven by structured status codes where available
(`api_error_status`, provider status) and by the executor's own error text
otherwise — never by guessing at success.

## 4. Managed clean execution source

Background work must not depend on — and must never "fix" — the user's
working copy. `app/background/managed_source.py` implements the contract:

- **Clean repo** → used directly, exactly as before.
- **Dirty repo** → served from a *managed mirror*: a dedicated clone under
  the worktree root, refreshed from the user's `HEAD` with a read-only
  `git fetch` and a mirror-side `reset`. The user's repo is only ever read.

Guarantees:

- no `git clean`, no `git reset`, no `git stash`, no checkout in the user's
  working copy — ever;
- uncommitted user changes are **not** silently copied into the mirror;
- the mirror's provenance (`source_repo`, `source_head`) is recorded on the
  task and emitted as `source.mirrored`;
- a repo that cannot be mirrored fails fast with a truthful error.

This is what makes the previously-failing worker tasks (Startup, Marketing,
Business) survive a dirty checkout without touching it.

## 5. Planner deadlines

Planning is bounded on three levels so a task can never sit in `PLANNING`
forever:

1. per-backend call timeouts inside the planner chain
   (`grok-cli` → `9router` → `local`),
2. an explicit planner repair round for malformed JSON rather than an
   open-ended retry,
3. the task's own wall-clock deadline, which the planner's cancel check
   observes between backends.

The planner backend that actually served each plan is persisted in
`result.planner_attempts`, so "which brain wrote this plan" is auditable.

## 6. Timeouts

| Knob | Default | Notes |
|---|---|---|
| `KODGAR_TASK_TIMEOUT` | 1800 s | default per-task wall clock |
| `KODGAR_SUITE_TIMEOUT` | 1500 s | cap for one verification pass |
| request `timeout_seconds` | — | per-task override (60 s – 4 h) |

The suite cap exists because real repositories have slow suites: Kodgar's
own suite takes ~15 minutes on a loaded machine, and the previous 600 s cap
guaranteed a `SIGTERM` mid-suite and a false failure. The cap is still
bounded by the task deadline, so a hung suite is killed rather than
allowed to run forever.

## Tests

`tests/test_executor_health.py` covers distinct-executor retry, the attempt
bound, circuit open/recovery, no-infinite-loop, failure-history
persistence, final-state-reflects-final-executor, and managed-source
behaviour. `tests/test_background_tasks.py` continues to cover the
lifecycle, event ordering, cancellation, restart, and no-fake-completion
contracts.
