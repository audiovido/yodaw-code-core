# Worker J: resilience, security, and failure-recovery audit (verified 2026-09-10)

## Scope
Audit and harden failure paths without broad feature work.
New tests: `tests/test_resilience_audit.py`,
`tests/test_security_audit.py`, `tests/test_failure_recovery.py`.
Minimal fixes only in `app/llm/provider.py`,
`app/storage/sqlite_store.py`, `app/storage/pg_store.py`.

## Bugs found (each reproduced by a failing test first)

1. **Concurrent duplicate submissions all succeeded (SQLite).**
   `MissionStore.enqueue()` ran the single-flight SELECT and the
   INSERT as separate autocommit statements. Four threads submitting
   the identical work item produced 4 missions instead of 1 + 3
   `DuplicateMission` rejections. Fixed by wrapping check and insert
   in one `BEGIN IMMEDIATE` transaction with an `IntegrityError`
   fallback that reports the winning row. Postgres `enqueue()` got
   the same fallback (its `FOR UPDATE` check already serialized, but
   the insert path had no race loser handling). Tested by
   `test_concurrent_duplicate_submissions_single_flight`.

2. **Outbox `pending` stat double-counted dead-lettered messages.**
   `outbox_stats()` counted every `delivered_at IS NULL` row as
   pending, including dead-lettered ones (quarantined for operator
   review, never deliverable). Stats read `pending=1` while
   `outbox_pending()` correctly returned `[]`. Fixed in both SQLite
   and Postgres adapters: pending now means undelivered AND not
   dead-lettered. Tested by
   `test_pending_stat_excludes_dead_lettered` and
   `test_poison_message_dead_letters_after_bounded_attempts`.

3. **HTTP 408 Request Timeout was not classified retryable.**
   `_is_retryable()` retried 5xx and 429 but not 408, even though a
   server-side request timeout means the call never ran to
   completion and is safe to retry. Fixed: 408 is retryable.
   Auth failures (401/403) stay fail-fast. Tested by
   `test_408_request_timeout_is_retryable` and
   `test_408_then_success_recovers`.

4. **One corrupt mission row wedged the whole claim/watchdog path.**
   `claim_next()` and `stale_executing()` called
   `Mission.model_validate_json()` with no guard: a single corrupt
   payload raised, so no mission could be claimed and no stale
   mission could be recovered. Fixed in both adapters: unparseable
   rows are skipped, left in place for operator inspection, and the
   scan continues to healthy candidates. Tested by
   `test_claim_skips_corrupt_row_and_claims_good` and
   `test_watchdog_skips_corrupt_row_and_recovers_good`.

## Verified intact (pinned by new tests, no fix needed)

- **Idempotency races**: concurrent idempotent outbox enqueues return
  one row (`test_concurrent_idempotent_enqueue_single_row`); two
  relays deliver once with a single ack winner
  (`test_two_relays_deliver_once_single_winner`).
- **Retry after partial failure**: 429/500/408-then-success recover;
  partial attempt evidence survives watchdog recovery
  (`test_retry_after_partial_failure_keeps_attempt_evidence`).
- **Worker crash/restart**: crash finalizes FAIL, releases the lease,
  clears inflight, and a restart re-acquires immediately
  (`test_worker_crash_finalizes_and_releases_lease`).
- **Outbox enqueue failure never fails the mission**
  (`test_outbox_enqueue_failure_does_not_fail_mission`).
- **Cancel vs complete**: completed missions return `terminal`;
  concurrent queued cancels produce exactly one `cancelled` + one
  `mission.cancelled` event
  (`test_completed_mission_cancel_returns_terminal`,
  `test_concurrent_cancel_on_queued_is_exact_once`).
- **Stale lease/claim recovery**: stale leases are stolen safely,
  heartbeats and releases are owner-guarded, `release_all` is scoped
  to the caller (`test_stale_lease_is_stolen_by_new_owner`,
  `test_lease_heartbeat_guarded_by_owner`,
  `test_release_all_only_releases_own_leases`).
- **Malformed provider responses**: invalid JSON bodies and empty
  OpenAI `choices` fail fast with one attempt
  (`test_invalid_json_body_is_not_retried`,
  `test_openai_malformed_choices_not_retried`).
- **Evidence preservation**: failed-mission evidence and events
  survive store reopen
  (`test_failed_mission_evidence_survives_reopen`).
- **Audit-chain integrity**: forgery and deletion detected at the
  exact seq; 100 threaded appends keep a verifiable chain
  (`test_audit_verify_detects_forged_row`,
  `test_audit_verify_detects_deleted_row`,
  `test_audit_appends_are_thread_safe_and_chain_intact`).
- **Tenant isolation**: foreign reads are 404 (never 403), listings
  exclude foreign missions, clients cannot reach audit/outbox
  surfaces (`test_foreign_mission_reads_are_404_not_403`,
  `test_client_cannot_reach_global_audit_trail`,
  `test_client_cannot_reach_outbox_surface`).
- **Oversized payload rejection**: goal/metadata boundaries pinned at
  the exact limit; rejections are audited; absurd capabilities fail
  closed to BLOCKED (`test_goal_length_boundary_exact_limit_ok`,
  `test_api_rejects_oversized_goal_with_413`,
  `test_oversized_rejection_is_audited`,
  `test_api_rejects_oversized_capability`).
- **Invalid state transitions**: unknown missions 404, double cancel
  409, invalid admin roles rejected, priorities clamped
  (`test_cancel_unknown_mission_returns_404`,
  `test_double_cancel_returns_409_terminal`,
  `test_invalid_admin_role_rejected`,
  `test_priority_out_of_range_is_clamped`).
- **Temp worktree lifecycle**: success removes, failure keeps clean
  for debugging, cleanup failure never hides the result
  (`test_successful_mission_removes_worktree`,
  `test_failed_mission_keeps_worktree_clean_for_debugging`,
  `test_cleanup_failure_does_not_hide_mission_result`).
- **Partial git apply / dirty repo**: invalid second edit applies
  nothing; dirty repos refuse before any mutation; path escapes
  rejected before any write
  (`test_partial_apply_second_edit_invalid_applies_nothing`,
  `test_dirty_repo_refuses_before_any_mutation`,
  `test_path_escape_rejected_before_any_write`).
- **Watchdog never clobbers terminal missions**: a PASS with a stale
  heartbeat is left intact
  (`test_watchdog_skips_terminal_mission_with_stale_heartbeat`).

## Out of scope / not changed

- HTTP request body-size cap (`max_body_bytes` in governance config)
  is declared but not enforced by middleware; payload limits are
  enforced per-field. Left as a documented gap, not a silent change.
- Watchdog recovery only inspects and fails stale missions; there is
  deliberately no automatic retry/resubmit path.
- Postgres race fallbacks are code-reviewed but not live-tested here
  (no server in this environment; hermetic pg contract tests pass,
  server tests skip).
