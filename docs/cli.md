# YODAW native interactive shell

The `yodaw` command opens an interactive coding-agent shell. The CLI is a
presentation and orchestration surface only: goals flow through the
existing pipeline (GOAL → OBSERVE → REPO INTELLIGENCE → PLAN → ROUTE →
EXECUTE → VERIFY → RECOVER → LEARN → DELIVER) in `app.planning`,
`app.repo_intelligence`, `app.workers`, and `app.core`.

## Launch

```bash
python -m app.cli            # interactive REPL (inside a repo)
python -m app.cli run "fix failing tests"
python -m app.cli status
python -m app.cli resume [session_id]
python -m app.cli sessions
python -m app.cli version
```

A `yodaw` executable is intentionally not installed by this change; run
through `python -m app.cli` so packaging stays untouched.

## REPL

Prompt: `YODAW > `. Multi-turn history, `Ctrl+C` interruption without
session loss, `Ctrl+D` graceful exit. One failed task never crashes the
session; evidence and conversation persist to `~/.yodaw/sessions/`.

Slash commands (never sent to the model): `/status /plan /diff
[/tests /evidence /model /provider /context /repo /history /resume
/cancel /clear /approval /help /exit`. Bare `cancel`, `clear`,
`status`, `history`, `resume`, `help`, and `exit` also work.

## Approval modes

- `safe`: every mutating task asks first.
- `standard` (default): code and test changes proceed; high-risk
  actions (`reset`, `rm -rf`, forced push, branch deletion) ask first.
- `auto`: bounded autonomous execution; high-risk actions still ask.

Existing safety policy is never bypassed. Dirty repositories are
reported at startup and never silently modified; `git reset`/`clean`
never run automatically.

## Non-interactive mode

```bash
python -m app.cli run "fix failing tests" --repo PATH --json --verbose \
  --model auto --provider auto --approval-mode standard --timeout 600
```

Exit codes: `0` task completed (PASS); `1` task failed, blocked,
cancelled, or interrupted; `2` usage error or unresumable session.

`--json` emits newline-delimited structured events and the final
result with no ANSI codes, suitable for CI and scripts.

## Manual smoke test (safe, read-only)

```bash
rm -rf /tmp/yodaw_smoke && mkdir /tmp/yodaw_smoke && cd /tmp/yodaw_smoke
git init -q && git config user.email t@t && git config user.name t
echo 'print("hi")' > app.py && git add . && git commit -qm init
python -m app.cli
> explain this repo
/status
/context
/exit
```

Expected: startup shows repo, branch, and clean state; `explain this
repo` summarizes without modifying files; `git status --short` stays
empty afterwards.
