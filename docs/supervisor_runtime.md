# Supervisor runtime (durable long-running tasks)

Worker N scope: `app/supervisor/`, `tests/test_supervisor_*.py`.

## Purpose

Run tasks for hours, survive process interruption and restart, and
resume without losing state or duplicating committed work.

## Job model

Each supervised job tracks `job_id`, optional `mission_id`,
`state`, `stage`, `step`, `attempt`, `created_at`, `started_at`,
`heartbeat_at`, `updated_at`, `finished_at`, `worker_id`, lease
owner and expiry, checkpoint and evidence references, and the
last error classification.

States: `QUEUED`, `CLAIMED`, `RUNNING`, `PAUSED`, `RECOVERING`,
`BLOCKED_EXTERNAL`, `FAILED`, `CANCELLED`, `PASS`.

## Leases and heartbeats

A worker claims a job inside one atomic transaction and receives
a bounded lease. Heartbeats extend the lease. A second worker
cannot execute a job under a valid lease. An expired lease is
recoverable by another worker. A stale owner cannot finalize a
job after losing its lease: every mutating call re-checks
ownership and expiry first.

## Crash recovery

On restart the new supervisor instance discovers non-terminal
jobs, leaves jobs with live leases alone, and moves stale jobs
to `RECOVERING`. Resumption reads the latest checkpoint
(`OBSERVE`, `PLAN`, `EXECUTE_STEP`, `VERIFY`, `RECOVER`) and
continues from its stage and step. Committed steps are never
re-run blindly: `complete_step` checks prior checkpoints for the
action key first.

## Exactly-once effect safety

True exactly-once is impossible for external side-effects, so
the supervisor implements idempotent-at-least-once semantics:
before repeating a side-effect, inspect prior evidence and the
latest checkpoint, detect whether the action key already
committed, and skip duplicates.

## Pause, resume, cancel

`pause` sets persistent pause state; no new step starts and the
in-flight step completes safely. `resume` moves the job to
`RECOVERING` so it continues from its checkpoint. Cancellation
is monotonic: once `CANCELLED`, no later `PASS` is possible and
no stale worker may overwrite the terminal state.

## Provider outage and backoff

Provider failure parks the job in `BLOCKED_EXTERNAL` with its
checkpoint retained; `unblock_provider` resumes after recovery.
Retryable failures use bounded exponential backoff with
configurable max retries and a persisted retry reason. There are
no infinite retry loops.

## Observability

Structured events per job: `JOB_CREATED`, `CLAIMED`,
`HEARTBEAT`, `CHECKPOINT`, `PAUSED`, `RESUMED`,
`LEASE_EXPIRED`, `RECOVERED`, `RETRY`, `CANCELLED`, `PASSED`,
`FAILED`. Events carry no secret values.

## Safe shutdown

`shutdown` stops accepting new claims and preserves active
state. Active jobs keep their leases and checkpoints so a later
instance recovers them; shutdown never marks active jobs failed
merely because the process exits.

## Storage

SQLite backend reusing the hardened connection policy from
`app.storage.db`. Tables (`supervisor_jobs`,
`supervisor_checkpoints`, `supervisor_events`) are separate from
the mission store, so no existing schema is touched.
