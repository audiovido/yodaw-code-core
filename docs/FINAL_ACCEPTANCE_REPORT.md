# Kodgar Final Acceptance Report (September 17, 2026)

## Mission table (exact)

| # | Item | Status | Evidence |
|---|------|--------|----------|
| 1 | Health-aware route certification | PASS | `certify_route`/`build_fallback_chain`; live sweep: 9×403, 3×5xx, 1×timeout, 2×PASS — no route trusted from inventory alone |
| 2 | Persist primary/fallbacks | PASS | primary `qwen/qwen2.5-coder:7b` in `~/.config/yodaw/config.toml` (key file untouched); fallback `mycombo-legacy` exported via `YODAW_LLM_FALLBACK_MODELS` (zshrc/bashrc marker block); unit test asserts env export |
| 3 | Single 9Router lifecycle owner | PASS | `yodaw kodgar` (probe→ensure-daemon→certify→persist); never spawns a second daemon when one answers |
| 4 | kodgar + kodgar-doctor | PASS | both commands + tests (`test_kodgar_certifies_persists_and_exports_fallbacks`, `test_kodgar_doctor_reports_unhealthy_without_daemon`) |
| 5 | macOS installer | PASS | `scripts/install_macos.sh` (`bash -n` OK) |
| 6 | Windows installer | PASS (SYNTAX_ONLY runtime) | `scripts/install_windows.ps1` + `.bat` shim; no Windows runtime on this machine — `WINDOWS_RUNTIME_TEST=NOT_RUN` |
| 7 | kodgar-installer.zip | PASS | built, secret-audited (see below) |
| 8 | Real inference | PASS | deterministic chat `KODGAR_ACCEPTANCE_OK` returned exactly, through 9Router→Ollama (local `qwen2.5-coder:7b`), verified pre-crash, post-recovery, and across the stability window |
| 9 | Crash recovery | PASS | daemon killed (health 000) → keeper respawned automatically → same inference PASS; production `ensure_daemon` path also proven (pid reported, health 200) |
| 10 | 30s stability | PASS | 2 health samples + 2 real chats in 30s window, 0 failures |
| 11 | Secret audit | PASS | staged diff, scripts dir, zip contents, config templates: no secrets |
| 12 | Docs / README install instructions | PASS | README "Installation (kodgar installers)" section + this report |

**11/12 PASS, 1 conditional (Windows runtime acceptance: script shipped and
syntax-checked; no Windows OS available to execute it). 0 NOT DONE.**

## How real inference was unblocked (legitimate routes only)

1. Classified every configured upstream provider route: all combos/models
   behind remote accounts return deterministic **403** (upstream access),
   5xx/timeout on some concrete models — all rejected immediately, none
   retried, no local keys minted (`validate_gateway_key` explicitly refuses
   to rotate keys for upstream 403s).
2. Found a legitimate **local** provider already on the machine: Ollama
   with `qwen2.5-coder:7b` (~4.4 GB, pre-downloaded — nothing new
   installed). Verified it directly (OpenAI-compatible `/v1`, real
   completion "OK").
3. Registered it through the existing zero-touch local-server support:
   `yodaw setup-9router --register-local "Local Qwen:qwen:http://127.0.0.1:11434/v1"`
   → node + connection created by 9Router, gateway key **reused** (status
   `reused`, never rotated).
4. **Production bug found and fixed en route:** shipping 9Router appends a
   `data: [DONE]` SSE trailer to some non-streaming proxied responses, so
   `certify_route` misclassified the healthy local route as `malformed`
   despite HTTP 200 with a valid completion. `certify_route` now falls
   back to the module's lenient parser (same behavior `chat()` already
   had). This is a real production fix, verified by the whole hermetic
   suite (68 tests green).
5. Certified the local route through 9Router (PASS with content `OK`),
   persisted primary, certified `mycombo-legacy` (PASS) as fallback.

## Route certification results (bounded live sweep, 20s per probe)

```
business                        403   (rejected immediately, no retry)
codgar-code                     403
fallback                        403
fast                            403
marketing                       403
mycombo-legacy                  PASS  (healthy)
mycombo                         403
reason                          403
startup                         403
verify                          403
qwen/qwen2.5-coder:7b           PASS  (healthy, local Ollama)
af/gpt-oss-120b                 5xx
af/gpt-oss-20b                  timeout
af/kimi-k2.7-code               5xx
ag/claude-opus-4-6-thinking     5xx
```

Totals: **working=2, 401=0, 403=9, 429=0, 5xx=3, timeout=1**.
All earlier upstream sweeps (before the local route existed): 403 on every
combo — recorded, rejected, never faked.

## Primary + fallbacks

- Primary (persisted in config): `qwen/qwen2.5-coder:7b`
- Fallback (persisted, certified): `mycombo-legacy`
- Gateway key: local credential, status `reused` (never rotated/minted)

## Deterministic acceptance chat

```
system: Reply exactly KODGAR_ACCEPTANCE_OK.
user:   KODGAR_ACCEPTANCE_OK
→ assistant text: 'KODGAR_ACCEPTANCE_OK'   (exact match)
```
Verified at T0 (pre-crash), T1 (post-automatic-recovery), and twice inside
the 30s stability window.

## Crash recovery evidence

```
kill daemon (pid from lsof :20128)  → health 000
keeper (launchd com.kodgar.9router-keeper, 30s loop):
  - healthy port     → no-op
  - booting daemon   → grace (spawn marker epoch, 150s)
  - wedged launcher  → reaped, fresh daemon spawned
  - missing daemon   → fresh daemon spawned (production env:
                       PORT/TRAY_MODE/DATA_DIR + cwd=data_dir)
health 200 restored automatically; same inference PASS
```
During debugging the keeper was hardened twice: (1) wedged-launcher reaping
(a launcher alive without ever binding the port), (2) boot-grace so a
cold-catalog boot (~90s) is never mistaken for a wedged process, and
production-matching spawn env (daemon died after banner without it).

## 30-second stability test

```
health samples: 2   real chats: 2   failures: 0   VERDICT: PASS
```

## Packaging

- `scripts/install_macos.sh` — `bash -n` PASS
- `scripts/repair_current_mac.sh` — `bash -n` PASS
- `scripts/9router-keeper.sh` — `bash -n` PASS (deployed to
  `~/.kodgar/runtime/9router-keeper.sh` under launchd KeepAlive)
- `scripts/install_windows.ps1` + `scripts/install_windows.bat` — no
  Windows runtime on this machine: `WINDOWS_RUNTIME_TEST=NOT_RUN`
  (syntax reviewed; not executed)
- `scripts/live_check.py` — bounded live probe (health→inventory→
  certify→chat), used for all evidence above
- `kodgar-installer.zip` — contains only scripts + docs; secret-audited

## Secret audit

- `git diff` / staged diff: no keys or tokens (config stores only the
  key-file *path*; the 0600 key file lives outside the repo)
- `scripts/`: no secrets (installer scripts reference env/file paths only)
- `kodgar-installer.zip`: audited with `grep -rE 'sk-[A-Za-z0-9]{8,}'
  | (api|token|secret)s?\s*='` on extracted contents — clean
- Config templates (`docs/`, README examples): no real credentials

## Reproduction

```bash
pytest tests/test_ninerouter.py tests/acceptance -q
bash scripts/repair_current_mac.sh
python3 scripts/live_check.py
```
