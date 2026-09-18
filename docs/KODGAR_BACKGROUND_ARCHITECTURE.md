# Kodgar Background Architecture (V1)

Kodgar is a **background autonomous coding system**. The web Terminal and the
CLI are two clients of one Task API; there is exactly one execution engine.

```
REACT TERMINAL ─┐
                ├──► TASK API (/api/v1/tasks) ──► BACKGROUND ENGINE ──► EXECUTOR
CLI (kodgar task)┘         │                          │                  (claude-code │ codex │ grok-cli │ kodgar-native)
                           │                          ▼
                           │                    GROK PLANNER (grok-cli → 9router → local)
                           │                          │
                           │                    isolated git worktree
                           │                          │
                           │                    KODGAR VERIFIER (authority)
                           │                          │
                           └── SSE /api/v1/tasks/<id>/events ◄── event bus + SQLite
```

## Module map (`app/background/`)

| Module | Role |
|---|---|
| `models.py` | Pydantic models: `TaskRequest`, `PlanResult`, `TaskRecord`, `ExecutionOutcome`, `VerificationReport`, state enum |
| `store.py` | Durable SQLite store (WAL, shared `app.storage.db` hardening, single-writer lock). Tasks, events, logs, cancel flags |
| `events.py` | `EventBus` — persisted pub/sub with replay (`stream(task_id, after_seq)`) |
| `planner.py` | Grok Architect: strict-schema JSON plan, backend chain `grok-cli → 9router → local`, one recorded repair round |
| `repo_intel.py` | Repo intelligence (languages, frameworks, tools, models) fed to the planner |
| `executors/` | `base.py` contract + `claude_code.py`, `codex.py`, `grok_cli.py`, `kodgar_native.py`, `registry.py` |
| `verifier.py` | The authority: worktree, diff scope, syntax, tests, build, acceptance, commit evidence |
| `engine.py` | Background job engine: threads, timeouts, cancellation, PID tracking, crash recovery, worktree lifecycle |
| `api.py` | FastAPI router: submit, list, detail, events (SSE), cancel, executors, diff |
| `service.py` | Wiring: engine + store + bus startup, recovery on boot |

## Task lifecycle

`QUEUED → PLANNING → PLANNED → PREPARING → CODING → TESTING → VERIFYING → COMMITTING → COMPLETED`

failure paths: `BLOCKED`, `FAILED`, `CANCELLED` (any stage).

Invariants:

- **COMPLETED requires physical evidence.** A textual LLM answer is never
  success: the verifier must observe filesystem mutation, passing checks and a
  real commit before the state flips.
- State and events are persisted **per transition**; every task's full event
  history lives in SQLite, so any client can reconnect with `?after=<seq>` and
  replay exactly.
- Terminal event ordering is atomic with the state flip on all terminal paths
  (an observer that sees `COMPLETED` can never miss `task.completed`).

## API surface (mounted at `/api/v1` by `app/main.py`)

| Method & path | Purpose |
|---|---|
| `POST /tasks` | Submit; returns `202 {task_id, state}` immediately — execution is background |
| `GET /tasks` | List (with counts); query `limit`, `state`, `project_id` |
| `GET /tasks/{id}` | Real status: state, progress (derived from pipeline stages), executor, planner backend, files changed, tests, commit, elapsed |
| `GET /tasks/{id}/events` | SSE stream; `?after=` / `Last-Event-ID` replay; `task.stream_end` frames |
| `POST /tasks/{id}/cancel` | Cooperative cancellation flag checked in all executors |
| `GET /executors` | Live availability + capabilities from the registry |
| `GET /tasks/{id}/diff` | Patch of the task branch vs base |

Auth/RBAC: `tasks.read` / `tasks.write` / `tasks.cancel` permissions, granted
per role in `app/api/rbac.py`. CORS is enabled in `app/main.py` so the Vite dev
server can talk to the API directly.

## Event vocabulary

`task.created`, `task.state`, `planner.started`, `planner.completed`,
`executor.selected`, `executor.started`, `executor.output`, `executor.pid`,
`executor.completed`, `worktree.created`, `file.changed`, `test.started`,
`test.completed`, `verification.started`, `verification.completed`,
`git.committed`, `task.completed`, `task.failed`, `task.cancelled`,
`task.blocked`.

Every event: monotonic `seq`, ISO timestamp, `task_id`, `type`, structured
payload. Stored in SQLite first, fanned out by the bus second.

## Planner contract

The planner receives goal + repo intelligence + available executors/models and
must return strict JSON (`PlanResult`): `task_type`, `summary`, `languages`,
`frameworks`, `tools`, `executor`, `reason`, `plan[]`, `acceptance[]`.
Malformed output gets **one recorded repair round**; persistent failure fails
the task (`BLOCKED`/`FAILED`) — never a silent default plan. The serving
backend (`grok-cli` | `9router` | `local`) is recorded as `planner_backend`.

9Router gateway keys resolve zero-touch: explicit `NINEROUTER_API_KEY` if it
validates, otherwise derive the local admin token and reuse/mint a gateway key
(`app.llm.ninerouter.resolve_admin` + `provision_gateway_key`).

## Persistence & restart safety

- SQLite at `KODGAR_TASKS_DB` (default `data/kodgar_tasks.db`), WAL mode,
  busy timeout, explicit transactions, one shared connection guarded by a lock.
- On boot, `store.recover_interrupted()` moves tasks that were mid-flight when
  the process died to `FAILED` with a recovery note; they are never left
  flapping as `CODING`.
- Executor subprocess PIDs are recorded (`executor.pid` events) for
  observability; in-process executors record no PID.

## Security / safety

- One isolated `git worktree` per task on branch `yodaw/task-<pid>-<n>-<ts>-<rand>`;
  worktree root must be **absolute** (default `~/.kodgar/worktrees`, absolute
  `KODGAR_WORKTREE_ROOT` override) so repo-cwd and server-cwd always agree.
- The user's checkout is never mutated: no reset, no clean, no force push,
  commits land only on the task branch.
- Bounded subprocesses (`app.workers.safe_subprocess`), no `shell=True`,
  timeouts at every stage, redacted logs, path validation on repo/worktree.
