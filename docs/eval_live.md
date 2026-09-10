# Live Evaluation (Worker H)

Live evaluation runs benchmark cases through the real coder execution
path (`RepoCodeWorker`) against a real model provider. It is separate
from the hermetic fake-provider suite, which stays the default for
`python -m app.eval run`.

## Quick start

```bash
export YODAW_EVAL_API_KEY="..."
export YODAW_ENABLE_GITHUB=false   # keep offline fixtures hermetic

python -m app.eval live \
  --provider openai-compatible \
  --model <model> \
  --base-url <url> \
  --case bugfix_basic \
  --output /tmp/live_report.json
```

Ollama-compatible providers:

```bash
python -m app.eval live \
  --provider ollama \
  --model <model> \
  --base-url http://127.0.0.1:11434
```

Use `--suite <tag>` to run a tag slice, or omit both `--case` and
`--suite` to run the full suite. Use `--artifact-dir <dir>` to keep
failed case worktrees for diagnosis.

## Score semantics

Live outcomes keep four signals distinct:

| Signal | Meaning |
| --- | --- |
| `HARNESS_PASS` | The harness ran correctly, whatever the outcome. |
| `PROVIDER_AVAILABLE` | The model answered with usable output. |
| `TASK_PASS` | The change passed verification and scoring. |
| `BENCHMARK_SCORE` | Numeric score for a completed task. |

A provider or network failure produces `BLOCKED_EXTERNAL`. That is a
provider outage classification, not a task failure: it is never
recorded as `TASK_FAIL` and never converted into a benchmark failure.

## Configuration

| Option | Environment | Notes |
| --- | --- | --- |
| `--provider` | `YODAW_EVAL_PROVIDER` | `openai-compatible` or `ollama`. |
| `--model` | `YODAW_EVAL_MODEL` | Required. |
| `--base-url` | `YODAW_EVAL_BASE_URL` | Required. |
| `--api-key-env` | `YODAW_EVAL_API_KEY_ENV` | Name of the variable holding the key. Default `YODAW_EVAL_API_KEY`. |
| `--timeout-s` | `YODAW_EVAL_TIMEOUT_S` | Per-request timeout. |
| `--max-retries` | `YODAW_EVAL_MAX_RETRIES` | Bounded retries for 429/5xx/timeouts only. |

The API key is read from the environment on each request and is never
written to logs, reports, or evidence. Temperature is fixed to `0`
where the provider supports it.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All selected tasks passed. |
| `1` | At least one task failed. |
| `2` | Provider blocked (no task passed) or invalid configuration. |
