# API-E Final Graduation — Real Repo-Code Path (2026-09-11)

Candidate: local integration branch `yodaw/integration-api-release` @ `aa8dec6`
(`fix(api): report and enforce the instantiated storage backend (P1)`).
Provenance: full 14-stage acceptance rerun against pristine detached worktree at `aa8dec6`.

## Integration audit (required dependencies)

- API B reliability (`8d8464c`): IN candidate.
- API D security (`222346c`): IN candidate.
- API C evidence/readiness (`367de0f` + `f840cdb`): IN candidate.
- API A admission contract (`941ba96`, `tests/test_api_contract.py`): NOT in candidate.
- API E E2E (`9d094c8`): IN candidate.

`origin/master` (`d89e8b2`) predates the candidate. No `origin/yodaw/integration-api-release`
remote branch exists; the candidate is local-only, based on master plus the above.

## Supported release scope

Trusted single-node operation only. The default server posture with no
`YODAW_REPO_ROOTS` configured accepts any local `repo_path` (verified live).
Repository confinement holds only when `YODAW_REPO_ROOTS` is set at startup
(verified live: outside/`..`/symlink → 403, allowed alias → 200). Broader or
multi-tenant deployment is not proven.

## Acceptance result (all via real HTTP server, disposable temp Git repo)

- START SERVER: `python -m app.runtime`, profile `local`, SQLite, embedded coordinator.
- READINESS: `READY`, `sqlite`, `repository_bound:{restricted:False,root_count:0}`; caps include `repo-code-bud`.
- AUTH: local-dev open 200; single-node without key refuses startup (exit 3); with key, no-key/wrong-key 401, right-key 200.
- SUBMIT: `repo-code` with real metadata edit (`calculator.py` += `multiply`), no worker internals.
- STATUS: polled `QUEUED → EXECUTING → PASS`, `worker: repo-code-bud`.
- RESULT/GIT: branch `yodaw/task-*` commit exists, file content correct, source `HEAD` unchanged at baseline.
- EVIDENCE: 15 items (`edit`, `pytest`, `worktree_cleanup`) + 9 events (`mission.queued`…`mission.completed`).
- IDEMPOTENCY: replay with known key returns same `mission_id` + `replayed:true`.
- CANCEL: queued → `CANCELLED`; finished → 409.
- RETRY: `PASS` → 409; `BLOCKED` → new `QUEUED` with lineage.
- RESTART/RECOVERY: restart on same DB, mission + evidence coherent.
- TENANT/REPO DENIAL: unknown mission → 404; cross-tenant isolation covered by `test_rbac.py`;
  live repo-bound 403s (`outside`, `..` escape, symlink) with `YODAW_REPO_ROOTS` set; alias of allowed → 200.
- FAILURE PATH: missing target file → terminal `FAIL`, `TargetNotFound` with non-empty evidence.
- CLEAN SHUTDOWN: SIGTERM, exit 0.
- PROCESS CLEANUP: none left on probe ports.

## Regression (pristine `aa8dec6`)

- Targeted security/boundary: `104 passed` (`test_auth_fail_closed`, `test_repo_boundary`,
  `test_rbac`, `test_storage_contracts`, `test_body_limit`, `test_deployment_truth`,
  `test_governance_and_profiles`).
- Full suite: `831 passed, 7 skipped` on pristine `aa8dec6` (114s).

## Verdict

`WAITING_FOR_INTEGRATION` — the real repo-code path passes end-to-end on the candidate,
but the required API A admission contract (`941ba96`) is not merged into it, so final
graduation cannot be declared.
