# Multi-Agent Scheduler

Production DAG-aware scheduler on top of the durable supervisor
lease semantics. Pipeline:

`GOAL -> PLAN DAG -> SCHEDULER -> ELIGIBILITY -> CAPACITY -> CLAIM
-> EXECUTE -> VERIFY -> RETRY/RECOVER -> DEPENDENCY RELEASE
-> COMPLETE`

## Modules

- `app/scheduler/models.py`: task, worker, merge, and event models.
- `app/scheduler/store.py`: SQLite persistence with atomic claims.
- `app/scheduler/scheduler.py`: readiness, dispatch, retry,
  preemption, recovery, and merge coordination.

## Concepts

### DAG scheduling

Plans submit as task lists with `dependencies`. Unknown
dependencies fail intake; cycles raise `CycleError`. Tasks move
`QUEUED -> BLOCKED -> READY` as parents complete, and failed or
cancelled parents fail dependents with `DependencyFailed`. Release
is deterministic: Kahn order with creation-sequence tie-break.

### Capacity

Workers register concurrency limits and capability sets. Dispatch
checks worker load first, then the global limit, then capability
eligibility. Over-allocation is impossible because assignment
requires an atomic store claim.

### Priority and aging

Lower `priority` numbers dispatch first; ties break FIFO by
creation sequence. Every waiting round increments `wait_rounds`,
and the effective priority improves once per `aging_threshold`
rounds, so low-priority work cannot starve.

### Leases

Ownership mirrors the supervisor: `BEGIN IMMEDIATE` claims,
heartbeat extension, and stale-lease recovery. Concurrent
schedulers serialize on the write lock, so one task has one
owner. `recover()` moves expired leases to `RECOVERING`, ready
for deterministic reassignment.

### Preemption

Only `preemptible` tasks in `ASSIGNED` or `RUNNING` preempt.
Checkpoints persist before requeue; terminal tasks always return
`None` and keep their final state.

### Retry policy

Bounded `max_retries` with exponential backoff metadata
(`retry-scheduled` records attempt and delay). Non-retryable
errors (`ValidationError`, `AuthError`, `Cancelled`,
`DependencyFailed`) fail immediately. Provider errors become
`BLOCKED_EXTERNAL`, which stays distinct and only resumes via
`unblock_external()`.

### Merge coordination

Completed tasks register merge candidates with patch identity
and touched files. Same patch id or overlapping files mark both
sides `CONFLICT`; conflicting patches never auto-merge and move
through `NEEDS_REVIEW -> MERGED/RESOLVED`.

### Events

Every transition persists evidence: `queued`, `ready`,
`assigned`, `preempted`, `retry-scheduled`, `recovered`,
`completed`, and `blocked`.

### Recovery

Process or scheduler restart reopens the same database. Expired
leases recover to `RECOVERING`; completed tasks stay complete;
blocked tasks keep waiting. Partially completed DAGs resume from
the ready frontier.
