# 9Router Acceptance Guide

Native 9Router support: YODAW speaks the 9Router OpenAI-compatible API
(`http://127.0.0.1:20128/v1`) with `stream=false`, detects models and
combos from `GET /v1/models`, persists the selection to the config file
(no manual env exports), and falls back to a second backend when
configured.

## 1. Install 9Router (once per machine)

```bash
npm install -g 9router
9router
```

The dashboard opens at `http://127.0.0.1:20128/dashboard`.

1. **Providers -> Connect** Kiro AI (~50 free credits/month) or OpenCode
   Free (no auth). At least one provider must be connected or 9Router
   reports zero models.
2. Copy the **API key** from the dashboard.

## 2. Zero-touch provisioning (no manual env exports)

```bash
export NINEROUTER_API_KEY='<key from the dashboard>'
yodaw setup-9router
```

Expected output:

```
9Router is ready:
  endpoint : http://127.0.0.1:20128
  inventory: 3 models, 1 combos
  combos   : premium-coding
  model    : premium-coding
  config   : /path/to/config.toml
  key      : set via NINEROUTER_API_KEY
  verified : True
```

This probes the daemon, picks the default (a coding combo when one
exists, else the best model), writes `[llm] provider/model/base_url`
to the config file, and verifies with one tiny chat. Re-run any time
to refresh the inventory. Flags:

- `--model <id|auto>` pin a model/combo (`auto` = detect every run)
- `--base-url <url>` non-default daemon address
- `--no-verify` persist without the verification chat
- `--json` machine-readable evidence

Manual equivalent (same result, three commands):

```bash
yodaw config set --provider 9router --model auto
yodaw config validate
yodaw models
```

## 3. Everyday commands

```bash
yodaw config show            # effective non-secret configuration
yodaw config validate        # fail fast on a bad file/key
yodaw models                 # combos + models + detected default
yodaw models --json
```

Selection precedence (unchanged): explicit env vars beat the file,
the file beats defaults. After provisioning, **no exports are needed**
for provider/model/endpoint; only the key stays in the environment
(secrets are never written to the file by design).

## 4. Fallback chain (optional)

```bash
export YODAW_LLM_FALLBACKS=ollama   # 9router -> ollama
```

Each style is tried once, in order, with its own defaults (the
primary's base_url/model never leak into the backup). Every hop is
recorded in the provider attempt log and becomes mission evidence.

## 5. SQLite / runtime repair

Fresh installs and corrupt files self-report with the fix:

```bash
python -m app.operations db-check --db data/yodaw.db
python -m app.operations db-repair --db data/yodaw.db
```

- missing file -> schema created fresh (`repaired: true`)
- healthy file -> no-op (`repaired: false`)
- corrupt file -> moved to `<name>.corrupt-<utc>.bak` (+ WAL/SHM
  sidecars) and the schema recreated; nothing is ever deleted
  silently (pass `--no-backup` to delete instead)

## 6. Acceptance harness

```bash
python scripts/ninerouter_e2e.py all            # everything
python scripts/ninerouter_e2e.py all --json     # evidence blob
python scripts/ninerouter_e2e.py probe|models|chat|mission|fallback|fresh-install|restart
```

Exit codes: `0` PASS, `1` FAIL, `2` SKIP/BLOCKED (daemon not running,
no key for a live check, or the sandbox cannot run the check). What
each step proves:

| Step | Proves |
|---|---|
| `probe` | daemon reachable, inventory + default detected |
| `models` | combos/models parse, deterministic pick |
| `chat` | one real chat round-trip (`stream=false`) |
| `mission` | full YODAW mission lifecycle ends PASS on 9Router |
| `fallback` | dead primary -> dead backup walks the chain, error surfaces |
| `fresh-install` | clean HOME + clean DB: repair, config, boot, `/health` READY |
| `restart` | real launcher `start -> restart -> stop` with health gates |

## 7. Human acceptance checklist

- [ ] `npm install -g 9router && 9router` shows the dashboard
- [ ] a provider is connected (inventory non-empty in `yodaw models`)
- [ ] `yodaw setup-9router` ends `verified : True`
- [ ] `yodaw config show` displays `provider = 9router` with no exports
- [ ] `python scripts/ninerouter_e2e.py all` is PASS (or SKIP with a
      stated reason on machines without the daemon)
- [ ] full `pytest` is green (modulo documented environment-only
      failures: `lsof`, `python3.12`, live-LLM seats)
- [ ] `./yodaw restart` returns to READY (covered by `restart`, or run
      by hand)
- [ ] fallback: stop 9Router with `YODAW_LLM_FALLBACKS=ollama` and a
      local Ollama running -> missions still PASS via ollama

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `cannot reach 9Router` | start the daemon: `9router` |
| `no models and no combos` | connect Kiro AI / OpenCode Free in the dashboard |
| `401` from chat | refresh `NINEROUTER_API_KEY` from the dashboard |
| `unable to open database file` | `python -m app.operations db-repair` |
| quota exhausted mid-run | 9Router auto-falls-back internally; YODAW retries transient 429/5xx, then `YODAW_LLM_FALLBACKS` |
| `model X is not in the current inventory` | provider disconnected; rerun `yodaw setup-9router` |

## 9. Design notes

- Wire format is plain OpenAI chat-completions with `stream=false`
  and `temperature=0` (`app/llm/ninerouter.py`); retries/backoff reuse
  the existing provider attempt log.
- Combos (bare ids like `premium-coding`) are preferred over concrete
  models (`kr/...`) because they route with automatic fallback.
- `model = "auto"` in the config means runtime detection (cached per
  process); explicit ids skip the inventory call entirely.
- Hermetic coverage lives in `tests/test_ninerouter.py` (44 tests):
  config, payload shape, inventory parsing, deterministic pick,
  fallback mechanics, SQLite repair, CLI provisioning, and a full
  mission lifecycle over a mocked 9Router endpoint.
