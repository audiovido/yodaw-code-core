# Kodgar Executor Contract

Every background executor implements the same normalized interface
(`app/background/executors/base.py`). Adapters call the **real installed
tool**; availability is probed, never assumed.

## Interface

```python
class BackgroundExecutor(Protocol):
    id: str                       # "claude-code" | "codex" | "grok-cli" | "kodgar-native"
    def available(self) -> tuple[bool, str]      # (is_usable, reason_if_not)
    def capabilities(self) -> dict                # advertised to planner + /executors
    def execute(self, ctx: ExecutorContext) -> ExecutionOutcome
    def cancel(self) -> None                      # cooperative; checked inside execute
    def status(self) -> dict
```

`ExecutorContext` carries: task, goal, plan, `repo` (user checkout — never
mutated), `worktree` (isolated), prompt, cancel flag, event emitter, timeout.

`ExecutionOutcome` is **evidence, not verdict**: `ok`, `changed_files`,
`stdout_tail`, `error`, `duration_seconds`. The Kodgar Verifier — not the
executor — decides success.

## Adapters

| id | Binary & invocation | Sandbox notes |
|---|---|---|
| `claude-code` | `claude -p <prompt> --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools <set> --add-dir <worktree>` | stream-json parsed for file changes + PID; tool allow-list |
| `codex` | `codex exec --cd <worktree> --json --sandbox workspace-write <prompt>` | JSON events parsed for changes + PID |
| `grok-cli` | `grok -p <prompt> --cwd <worktree> --output-format plain --permission-mode bypassPermissions --always-approve --no-subagents --max-turns 30` | bounded turns |
| `kodgar-native` | In-process engine: LLM edit plan → `app.workers.edit_engine` applies verified file ops in the worktree | no external binary; **all LLM calls time-bounded** |

All CLI executors: run via `safe_subprocess` (no shell), cwd = worktree,
hard timeout, cooperative cancel checks, structured `executor.output` events,
`executor.pid` when the tool exposes a process.

## Availability & selection

`registry.py` probes each executor (`--version` / config presence) at runtime
and exposes `GET /api/v1/executors`. An unavailable executor is reported and
skipped — one missing tool never fails the product.

Selection policy: the **Grok planner chooses** the executor from live
availability and repo intelligence, returning `executor` + `reason` in the
plan. Defaults steer: large multi-file implementation → `claude-code`; deep
debugging/repo analysis → `codex`; architecture/research → `grok-cli`;
simple/deterministic/private/offline → `kodgar-native`. These are defaults,
not hard-coded truths; user preference (`preferences.executor`) overrides.

## Verifier precedence

The executor is never the authority. After execution, the verifier checks the
worktree independently: existence, expected/out-of-scope diff, syntax, tests,
build, acceptance criteria, commit. Mutation tasks **must** show physical
filesystem changes; `COMPLETED` without real evidence is impossible by
construction.
