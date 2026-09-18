# Kodgar Web E2E Report

Date: 2026-09-18 · Verdict: **PASS** (26/26 scripted checks + browser UI + reconnect)

## What was proven

Full vertical slice against a disposable repo (`/tmp/kodgar_e2e_repo`, branch
`main` seeded with one README commit):

```
Browser/CLI → POST /api/v1/tasks → Grok planner (9router) → background engine
→ isolated worktree → kodgar-native executor → real file created
→ verifier PASS → real commit on task branch → SSE completion → UI result page
```

## Scripted run (`scripts/e2e_web.py`) — 26/26 PASS in 35s

- submit returns immediately (`202`, state `PLANNING`)
- task reached `COMPLETED` (not FAILED/BLOCKED, not a text-only answer)
- Grok planner JSON stored with reason and acceptance criteria
  (`planner_backend=9router`, `task_type=file_creation`)
- executor selected + started in background
- physical file created with **exact** content `KODGAR_WEB_E2E_OK`
  (`git show <sha>:KODGAR_WEB_E2E.txt`)
- real commit on an **isolated** task branch; `main` untouched; user worktree clean
- verification recorded `PASS`; `files_changed >= 1`
- SSE stream delivered the whole lifecycle: `task.created`, `planner.started`,
  `planner.completed`, `executor.selected`, `executor.started`,
  `verification.started`, `verification.completed`, `git.committed`,
  `task.completed` — with monotonic `seq`
- reconnect: status after reconnect restores real terminal state

## Browser UI run (Kodgar Terminal, Vite dev server)

1. Submitted `Create a file named UI_BROWSER_E2E.txt …` from the sidebar form.
2. Card appeared instantly in ACTIVE as `PLANNING`, streamed through
   CODING → TESTING → VERIFYING → COMMITTING → COMPLETED (00:30).
3. Terminal pane rendered the real event transcript (plan, executor, file
   change, verification PASS, commit line); inspector showed planner
   `9router`, verify `PASS`, branch, commit `fd21b6a`.
4. On-disk evidence confirmed: exact content, commit only on the task branch,
   `main` untouched.

## Reconnect test

Submitted a RECONNECT_E2E task from the UI, **reloaded the page mid-flight**.
After reload the UI restored the task from the store, replayed the full event
history over SSE (`?after=<seq>`), and displayed the completed result with
commit `ea66157` and verifier PASS. Execution never depended on the browser
connection.

## Real defects found & fixed during E2E

1. **Relative worktree root** — `worktree_root` defaulted to a relative path,
   so the executor (repo cwd) and verifier (server cwd) resolved *different*
   directories. Fixed: absolute worktree root required.
2. **Unbounded native edit-plan LLM call** — could hang a task for ~9 minutes.
   Fixed: hard timeout on that call.
3. **Stale 9Router credential path** — planner hard-failed the 9router backend
   when `NINEROUTER_API_KEY` was unset, silently demoting planning to the local
   model. Fixed: zero-touch key resolution (`resolve_admin` +
   `provision_gateway_key`); the 9router backend now serves plans again.
4. **SSE stream died after first event** — endpoint probed a nonexistent
   `client_state`, raising `AttributeError` mid-generator. Fixed; stream now
   ends only on terminal events or client disconnect.
5. **Frontend AbortSignal misuse** — `streamEvents` cleanup called
   `signal.abort()` on an `AbortSignal` (no such method), crashing
   `TerminalPane` unmount. Fixed: cleanup is type-safe; the hook aborts its
   controller.
6. **Terminal event ordering** — state flipped to terminal before the terminal
   event was committed; observers could see terminal state without
   `task.completed`. Fixed: atomic ordering + one final store re-drain on the
   reader side.
7. **Per-thread SQLite connections + WAL pragma race** — startup deadlock under
   concurrency. Fixed: single shared connection with an internal lock.

## Test suites

- `tests/test_background_tasks.py`: **37 passed** (planner schema/validation,
  executor registry & availability, lifecycle, persistence, event ordering,
  SSE reconnect, cancellation, timeout, worker failure, validation failure,
  no-fake-PASS, commit evidence, restart recovery, API contracts).
- Frontend: `npm run build` **PASS** (both `index.html` and `kodgar.html`
  entries).

## Environment notes

- grok-cli headless is currently rejected upstream (OpenCode free tier 403);
  the planner chain correctly recorded the failure and fell through to the
  9Router backend. Executor adapters remain real integrations and are probed
  live via `GET /api/v1/executors`.
