"""
Stage 8.2/8.3/8.5: mission execution coordinator.

Owns the async execution runtime:

- atomically claims QUEUED missions (single-flight guarantee)
- bounded concurrency (YODAW_MAX_CONCURRENT_MISSIONS)
- per-repository exclusion so two missions never mutate the same
  target repository concurrently, across threads and processes
- heartbeats for live missions and held repo leases
- watchdog recovery of stale executing missions after a crash
- clean shutdown that never leaves half-committed missions

Design notes:

- claim_next() is transaction-based in the store, so even two
  independent coordinator processes cannot receive the same
  mission. On top of that, repo leases prevent two *different*
  missions from mutating one repository concurrently.
- cancellation is cooperative: the API flips cancel_requested in
  the store; the worker observes it at checkpoints (before/after
  LLM calls, before edits, before validation, between repairs,
  before commit). The coordinator only waits for the worker to
  observe it, never kills threads mid-mutation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from app.core.models import Mission, MissionStatus, TERMINAL_STATUSES
from app.storage.sqlite_store import (
    InvalidStateError,
    MissionStore,
    StaleOwnerError,
)
from app.runtime.repo_leases import RepoLeaseManager, now_ts

logger = logging.getLogger("yodaw.coordinator")


def max_concurrent_missions() -> int:
    try:
        value = int(os.environ.get("YODAW_MAX_CONCURRENT_MISSIONS", "2"))
    except ValueError:
        value = 2
    return max(1, value)


def heartbeat_seconds() -> int:
    try:
        return int(os.environ.get("YODAW_HEARTBEAT_SECONDS", "30"))
    except ValueError:
        return 30


def stale_after_seconds() -> int:
    """Heartbeat age after which an executing mission is stale."""
    env = os.environ.get("YODAW_STALE_AFTER_SECONDS")

    if env:
        try:
            return max(10, int(env))
        except ValueError:
            pass

    return heartbeat_seconds() * 4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Coordinator:
    """
    Async mission executor.

    One coordinator can run in a thread (in-process with the API)
    or as its own process. Multiple coordinators are safe: claims
    are atomic, and repo leases keep same-repo missions serialized.
    """

    def __init__(
        self,
        store: Optional[MissionStore] = None,
        leases: Optional[RepoLeaseManager] = None,
        registry=None,
        id_prefix: str = "coord",
        relay=None,
        client_limits_provider=None,
    ):
        import app.workers.registry as registry_module

        self.store = store or MissionStore()
        self.leases = leases or RepoLeaseManager()
        self.registry = registry or registry_module.registry
        self.id = f"{id_prefix}_{uuid.uuid4().hex[:8]}"
        self.max_concurrent = max_concurrent_missions()
        self.heartbeat_interval = heartbeat_seconds()

        self._pool = ThreadPoolExecutor(
            max_workers=self.max_concurrent,
            thread_name_prefix=f"{self.id}-worker",
        )
        self._inflight: set[str] = set()
        self._inflight_repos: set[str] = set()
        self._inflight_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._shutting_down = False

        # Observability for the heartbeat loop: every failure is
        # counted and logged; a heartbeat loop must never die
        # silently.
        self._hb_beats = 0
        self._hb_failures = 0

        # Stage 9: optional per-client concurrency limits, resolved
        # lazily per claim pass so limit changes apply immediately.
        self.client_limits_provider = client_limits_provider

        # Stage 9: exactly-once outbox relay for learning records.
        from app.runtime.outbox_relay import OutboxRelay

        self.relay = relay or OutboxRelay(store=self.store)

    def stats(self) -> dict:
        """Runtime observability: heartbeat counters and thread state."""
        return {
            "coordinator": self.id,
            "heartbeat_beats": self._hb_beats,
            "heartbeat_failures": self._hb_failures,
            "loop_alive": bool(self._thread and self._thread.is_alive()),
            "heartbeat_thread_alive": bool(
                self._hb_thread and self._hb_thread.is_alive()
            ),
            "inflight": len(self._inflight),
        }

    # -----------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"{self.id}-loop",
            daemon=True,
        )
        self._thread.start()
        self.relay.start()

        # The heartbeat runs on a dedicated thread, independent of
        # claim dispatch and of worker execution: a long-running
        # worker or a slow store operation must never stop the
        # persisted heartbeat from advancing (Stage 8.5 hard
        # requirement).
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"{self.id}-heartbeat",
            daemon=True,
        )
        self._hb_thread.start()

    def stop(self, drain: bool = True, timeout: float = 30.0):
        """
        Clean shutdown: stop claiming, optionally drain inflight
        missions, release leases, close the executor.
        """
        self._shutting_down = True
        self._stop.set()
        self._wake.set()

        if self._thread:
            self._thread.join(timeout=5)

        if self._hb_thread:
            self._hb_thread.join(timeout=5)

        self.relay.stop(timeout=5)

        if drain:
            self._drain(timeout)

        self.leases.release_all(self.id)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _drain(self, timeout: float):
        import time

        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            with self._inflight_lock:
                if not self._inflight:
                    return
            time.sleep(0.1)

    def wake(self):
        """Notify the loop that new work may be available."""
        self._wake.set()

    # -----------------------------------------------------
    # Main loop
    # -----------------------------------------------------

    def _run_loop(self):
        """Claim/dispatch loop. Heartbeats and watchdog live elsewhere."""
        while not self._stop.is_set():
            try:
                claimed = self._claim_and_dispatch()
            except Exception:
                # An unhandled exception here would silently kill
                # the loop thread; surface it and keep the loop
                # alive.
                logger.error(
                    "claim/dispatch pass failed\n%s",
                    traceback.format_exc(),
                )
                claimed = False

            if not claimed:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
            else:
                time.sleep(0.05)

    def _heartbeat_loop(self):
        """
        Dedicated heartbeat loop.

        Persists mission heartbeats and repo-lease heartbeats on
        its own schedule, independent of worker execution and claim
        dispatch. Every failure is counted, logged with a full
        traceback, and never allowed to end the loop. The watchdog
        shares this cadence: a single owner prevents concurrent
        double-recovery of the same stale mission.
        """
        last_watchdog = 0.0

        while not self._stop.is_set():
            failures = 0

            try:
                failures += self._heartbeat_inflight()
            except Exception:
                failures += 1
                logger.error(
                    "heartbeat pass failed\n%s",
                    traceback.format_exc(),
                )

            if failures:
                self._hb_failures += failures

            self._hb_beats += 1

            now = time.monotonic()
            if now - last_watchdog >= max(self.heartbeat_interval, 5):
                last_watchdog = now
                try:
                    self.recover_stale_missions()
                except Exception:
                    logger.error(
                        "watchdog pass failed\n%s",
                        traceback.format_exc(),
                    )

            self._stop.wait(self.heartbeat_interval / 2)

    def _heartbeat_inflight(self) -> int:
        """
        Persist heartbeats for inflight missions and held leases.

        Returns the number of failures. One mission's failure must
        never prevent the others from being heartbeated, so each
        item is isolated and its failure is logged with a full
        traceback instead of being swallowed silently.
        """
        failures = 0

        with self._inflight_lock:
            items = list(self._inflight)

        for mission_id in items:
            try:
                self.store.heartbeat(mission_id, self.id)
            except Exception:
                failures += 1
                logger.error(
                    "heartbeat persist failed for mission %s\n%s",
                    mission_id,
                    traceback.format_exc(),
                )

        for repo_key in self.leases.held_by(self.id):
            try:
                self.leases.heartbeat(repo_key, self.id)
            except Exception:
                failures += 1
                logger.error(
                    "lease heartbeat failed for %s\n%s",
                    repo_key,
                    traceback.format_exc(),
                )

        return failures

    def _claim_and_dispatch(self) -> bool:
        with self._inflight_lock:
            capacity = self.max_concurrent - len(self._inflight)
            held = set(self._inflight_repos)

        if capacity <= 0 or self._shutting_down:
            return False

        limits = None

        if self.client_limits_provider is not None:
            try:
                limits = self.client_limits_provider() or {}
            except Exception:
                # Quota resolution must never break claiming; a
                # failed lookup means "no limits known".
                logger.error(
                    "client limits lookup failed\n%s",
                    traceback.format_exc(),
                )
                limits = {}

        mission = self.store.claim_next(
            self.id,
            skip_repo_keys=held,
            client_limits=limits,
        )

        if mission is None:
            return False

        repo_key = MissionStore._repo_key(mission)

        # Re-check capacity under the lock; a previous dispatch may
        # have consumed it while we were claiming.
        with self._inflight_lock:
            if len(self._inflight) >= self.max_concurrent:
                # Give it back: requeue for another coordinator or
                # our next loop iteration.
                self._requeue(mission)
                return True

            self._inflight.add(mission.id)
            self._inflight_repos.add(repo_key)

        future = self._pool.submit(self._execute, mission, repo_key)

        # Bound unbounded exception spam; _execute never raises.
        future.add_done_callback(lambda f: f.exception())

        return True

    def _requeue(self, mission: Mission):
        """Atomically requeue a mission that couldn't acquire repo lease."""
        self.store.transact_mission(mission.id, lambda m: self._requeue_inner(m))

    def _requeue_inner(self, mission: Mission) -> Optional[Mission]:
        if mission.status not in (MissionStatus.running, MissionStatus.observing, MissionStatus.planning, MissionStatus.executing, MissionStatus.verifying, MissionStatus.repairing, MissionStatus.recovering):
            return None
        mission.status = MissionStatus.queued
        mission.claimed_by = None
        mission.claimed_at = None
        mission.heartbeat_at = None
        return mission

    # -----------------------------------------------------
    # Execution
    # -----------------------------------------------------

    def _execute(self, mission: Mission, repo_key: str):
        import time

        store = self.store

        try:
            # Per-repo admission: without this lease we may not
            # mutate the repo. Serialize instead of rejecting.
            acquired = False
            deadline = time.monotonic() + 600

            while not acquired and time.monotonic() < deadline:
                acquired = self.leases.acquire(
                    repo_key,
                    self.id,
                    mission_id=mission.id,
                    stale_after_seconds=stale_after_seconds(),
                )

                if acquired:
                    break

                # Someone else holds the repo; wait our turn.
                time.sleep(0.25)

            if not acquired:
                self._requeue(mission)
                return

            # Cancellation may have arrived between claim and
            # execution; consult the store before running.
            fresh = store.get(mission.id)

            if fresh is None:
                return

            if fresh.cancel_requested:
                self._finalize_cancelled(fresh, repo_key)
                return

            worker = self.registry.find(mission.capability)

            if worker is None:
                fresh.status = MissionStatus.blocked
                fresh.result = {
                    "error": f"No worker for capability: {mission.capability}"
                }
                fresh.finished_at = now_ts()
                try:
                    fresh = self._owned_save(fresh)
                except (StaleOwnerError, InvalidStateError):
                    logger.error(
                        "stale owner blocked for %s", fresh.id
                    )
                    return
                return

            fresh.worker = worker.name
            try:
                fresh = self._owned_save(fresh)
            except (StaleOwnerError, InvalidStateError):
                logger.error("stale owner blocked for %s", fresh.id)
                return

            # Worker I product lifecycle: observe/plan/skills context is
            # recorded through the facade before the existing worker
            # executes. Stages are events only: the stored status stays
            # RUNNING until a terminal state, so internal readers that
            # poll for RUNNING keep working. Dry-run missions finish
            # here without execution.
            try:
                from app.mission.facade import run_product_lifecycle

                if fresh.metadata.get("dry_run"):
                    run_product_lifecycle(store, fresh, dry_run=True)
                    return
                store.record_event(
                    fresh.id, "mission.observing", attempt=fresh.attempt, data={}
                )
                store.record_event(
                    fresh.id, "mission.planning", attempt=fresh.attempt, data={}
                )
                repo_path = fresh.metadata.get("repo_path")
                if repo_path:
                    try:
                        from app.mission.facade import observe_repo

                        repo_context = observe_repo(repo_path)
                    except Exception:
                        repo_context = {"observed": False}
                else:
                    repo_context = {"observed": False}
                try:
                    from app.mission.facade import build_plan, select_skills

                    plan_info = build_plan(fresh.goal, repo_context)
                    skill_info = select_skills(fresh.goal, repo_context)
                except Exception:
                    plan_info, skill_info = {}, {}
                fresh = store.get(fresh.id) or fresh
                evidence = list(fresh.evidence)
                evidence.append(
                    {
                        "type": "product_context",
                        "repo": repo_context,
                        "plan": plan_info,
                        "skills": skill_info,
                    }
                )
                fresh.evidence = evidence
                try:
                    fresh = self._owned_save(fresh)
                except (StaleOwnerError, InvalidStateError):
                    logger.error("stale owner blocked for %s", fresh.id)
                    return
                store.record_event(
                    fresh.id, "mission.executing", attempt=fresh.attempt, data={}
                )
            except Exception:
                fresh = store.get(fresh.id) or fresh

            metadata = dict(fresh.metadata)
            metadata["mission_id"] = fresh.id
            metadata["_event_store"] = store

            store.record_event(
                fresh.id,
                "mission.started",
                attempt=fresh.attempt,
                data={
                    "goal": fresh.goal,
                    "capability": fresh.capability,
                    "coordinator": self.id,
                    "worker": worker.name,
                },
            )

            from app.workers.repo_code_worker import CancelContext

            ctx = CancelContext(fresh.id, store)

            # Workers expose two contracts: plain execute(goal)
            # (code-bud) and execute(goal, metadata) (repo-code).
            import inspect

            params = inspect.signature(
                worker.execute
            ).parameters.values()

            takes_metadata = any(
                p.name == "metadata" or p.kind == p.VAR_KEYWORD
                for p in params
            )

            try:
                if takes_metadata:
                    result = worker.execute(fresh.goal, metadata)
                else:
                    result = worker.execute(fresh.goal)
            except Exception as exc:
                # Worker-level contract violation (should be rare:
                # repo worker catches its own exceptions).
                failed_result = {
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                }
                try:
                    fresh = self._finalize_terminal(
                        fresh,
                        status=MissionStatus.failed,
                        result=failed_result,
                        evidence=fresh.evidence,
                        error_class="task",
                        event_data={"outcome": "FAIL", "error": str(exc)},
                        success=False,
                    )
                except (StaleOwnerError, InvalidStateError):
                    logger.error(
                        "terminal finalize rejected for %s: %s",
                        fresh.id,
                        traceback.format_exc(),
                    )
                except Exception:
                    logger.error(
                        "terminal finalize failed for %s\n%s",
                        fresh.id,
                        traceback.format_exc(),
                    )
                    return
                try:
                    self.relay.drain_once()
                except Exception:
                    logger.error(
                        "eager outbox drain failed\n%s",
                        traceback.format_exc(),
                    )
                return

            # Cancellation observed mid-flight by the worker?
            error_type = (result.get("error") or {}).get("type", "")

            if error_type == "Cancelled":
                cancelled_result = dict(result.get("output", {}))
                cancelled_result["error"] = result["error"]
                try:
                    fresh = self._finalize_terminal(
                        fresh,
                        status=MissionStatus.cancelled,
                        result=cancelled_result,
                        evidence=self._merged_evidence(
                            fresh.evidence, result.get("evidence", [])
                        ),
                        event_type="mission.cancelled",
                        event_data={"while": "executing"},
                        success=False,
                    )
                except (StaleOwnerError, InvalidStateError):
                    logger.error(
                        "cancel finalize rejected for %s: %s",
                        fresh.id,
                        traceback.format_exc(),
                    )
                except Exception:
                    logger.error(
                        "cancel finalize failed for %s\n%s",
                        fresh.id,
                        traceback.format_exc(),
                    )
                    return
                try:
                    self.relay.drain_once()
                except Exception:
                    logger.error(
                        "eager outbox drain failed\n%s",
                        traceback.format_exc(),
                    )
                return

            # Refetch the mission to ensure we have the latest state before proceeding.
            fresh = store.get(mission.id)
            if fresh is None:
                return

            # Worker I: verify stage, then classify so provider faults
            # surface as BLOCKED_EXTERNAL instead of task FAIL.
            # The verify marker is best-effort only; the terminal
            # finalize below is the single durable commit.
            fresh.status = MissionStatus.verifying
            try:
                from app.mission.facade import classify_error

                error_class = (
                    None
                    if result.get("success")
                    else classify_error(result.get("error"))
                )
            except Exception:
                error_class = None if result.get("success") else "task"
            if result.get("success"):
                terminal_status = MissionStatus.passed
                terminal_error_class = fresh.error_class
            elif error_class == "provider":
                terminal_status = MissionStatus.blocked_external
                terminal_error_class = "provider"
            else:
                terminal_status = MissionStatus.failed
                terminal_error_class = "task"
            terminal_result = dict(result.get("output", {}))
            if result.get("error"):
                terminal_result["error"] = result["error"]
            try:
                fresh = self._finalize_terminal(
                    fresh,
                    status=terminal_status,
                    result=terminal_result,
                    evidence=self._merged_evidence(
                        fresh.evidence, result.get("evidence", [])
                    ),
                    error_class=terminal_error_class,
                    event_data={
                        "outcome": terminal_status.value,
                        "commit_sha": terminal_result.get("commit_sha"),
                    },
                    success=bool(result.get("success", False)),
                )
            except (StaleOwnerError, InvalidStateError):
                logger.error(
                    "terminal finalize rejected for %s: %s",
                    fresh.id,
                    traceback.format_exc(),
                )
                return
            except Exception:
                logger.error(
                    "terminal finalize failed for %s\n%s",
                    fresh.id,
                    traceback.format_exc(),
                )
                return

            try:
                self.relay.drain_once()
            except Exception:
                # The relay thread retries pending messages; never
                # fail the mission over learning delivery.
                logger.error(
                    "eager outbox drain failed\n%s",
                    traceback.format_exc(),
                )

        except Exception as exc:
            # Never lose a mission silently.
            try:
                fresh = store.get(mission.id)
                if fresh and fresh.status not in TERMINAL_STATUSES:
                    fresh.status = MissionStatus.failed
                    fresh.result = {
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    }
                    fresh.finished_at = now_ts()
                    store.save(fresh)
            except Exception:
                pass

        finally:
            self.leases.release(repo_key, self.id)

            with self._inflight_lock:
                self._inflight.discard(mission.id)
                self._inflight_repos.discard(repo_key)

            self._wake.set()

    def _owned_save(self, mission: Mission) -> Mission:
        """Fenced intermediate write; stale owner aborts execution."""
        save_owned = getattr(self.store, "save_owned", None)
        if save_owned is None:
            self.store.save(mission)
            return self.store.get(mission.id) or mission
        save_owned(mission, self.id)
        return self.store.get(mission.id) or mission

    def _finalize_cancelled(self, mission: Mission, repo_key: str):
        mission.status = MissionStatus.cancelled
        mission.result = {
            "error": {
                "type": "Cancelled",
                "message": "cancelled before execution started",
            }
        }
        mission.finished_at = now_ts()
        self.store.save(mission)
        self.store.record_event(
            mission.id,
            "mission.cancelled",
            attempt=0,
            data={"while": "claimed_not_started"},
        )

    @staticmethod
    def _merged_evidence(prior: Optional[list], new: Optional[list]) -> list:
        """Preserve prior evidence, append new items, skip dups."""
        merged = list(prior or [])
        seen = set()
        for item in merged:
            try:
                seen.add(json.dumps(item, sort_keys=True))
            except Exception:
                seen.add(repr(item))
        for item in new or []:
            try:
                key = json.dumps(item, sort_keys=True)
            except Exception:
                key = repr(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _finalize_terminal(
        self,
        mission: Mission,
        *,
        status: MissionStatus,
        result: dict,
        evidence: Optional[list],
        error_class: Optional[str] = None,
        event_type: str = "mission.completed",
        event_data: Optional[dict] = None,
        success: bool = False,
    ) -> Mission:
        """One atomic commit: mission + event + learning outbox."""
        from app.learning.engine import record_id_default

        finalize = getattr(self.store, "finalize_mission", None)
        merged_evidence = self._merged_evidence(
            (mission.evidence or []), evidence
        )
        payload = {
            "mission_id": mission.id,
            "goal": mission.goal,
            "worker": mission.worker,
            "success": success,
            "evidence": merged_evidence,
            "result": result,
            "record_id": record_id_default(mission.id),
        }
        if finalize is None:
            mission.status = status
            mission.result = result
            mission.evidence = merged_evidence
            if error_class is not None:
                mission.error_class = error_class
            mission.finished_at = mission.finished_at or now_ts()
            self.store.save(mission)
            self.store.record_event(
                mission.id, event_type,
                attempt=mission.attempt, data=event_data or {},
            )
            self.store.outbox_enqueue(
                mission_id=mission.id,
                kind="learning.record",
                payload=payload,
                idempotency_key=f"learning:{mission.id}",
            )
            return self.store.get(mission.id) or mission
        finalized = self.store.finalize_mission(
            mission.id,
            owner=self.id,
            status=status,
            result=result,
            evidence=merged_evidence,
            error_class=error_class,
            event_type=event_type,
            event_data=event_data or {},
            outbox_kind="learning.record",
            outbox_payload=payload,
            outbox_idempotency_key=f"learning:{mission.id}",
        )
        return finalized

    # -----------------------------------------------------
    # Watchdog / recovery (Stage 8.5)
    # -----------------------------------------------------

    def recover_stale_missions(self) -> list[str]:
        """
        Find stale executing missions and fail them with an
        explicit InterruptedExecution error.

        A mission claimed by a live coordinator keeps its
        heartbeat fresh, so it is never touched. A mission whose
        owner died has no heartbeat; we inspect the target repo
        for the mission branch and report exactly what was left
        behind, never re-running blindly (avoiding duplicate
        commits).
        """
        recovered: list[str] = []

        for mission in self.store.stale_executing(stale_after_seconds()):
            # Never touch missions claimed by live coordinators.
            if mission.claimed_by == self.id:
                with self._inflight_lock:
                    if mission.id in self._inflight:
                        continue

            try:
                self._recover_one(mission)
                recovered.append(mission.id)
            except Exception:
                continue

        return recovered

    def _recover_one(self, mission: Mission):
        from pathlib import Path

        repo_path = mission.metadata.get("repo_path")

        inspection = {
            "type": "recovery_inspection",
            "mission_id": mission.id,
            "claimed_by": mission.claimed_by,
            "attempt": mission.attempt,
            "started_at": mission.started_at,
        }

        # Inspect the target repository for leftover state.
        if repo_path:
            repo = Path(repo_path)

            if repo.exists():
                branch = mission.result.get("branch") or ""
                commit = ""

                if branch:
                    import subprocess

                    probe = subprocess.run(
                        ["git", "rev-parse", "--verify", branch],
                        cwd=repo,
                        capture_output=True,
                        text=True,
                    )

                    if probe.returncode == 0:
                        commit = probe.stdout.strip()

                inspection["leftover_branch"] = branch or None
                inspection["leftover_branch_sha"] = commit or None

        mission.evidence.append(inspection)

        mission.status = MissionStatus.failed
        mission.result = {
            **mission.result,
            "error": {
                "type": "InterruptedExecution",
                "message": (
                    "coordinator died mid-mission; recovered by "
                    f"{self.id} without re-execution"
                ),
                "recovered_by": self.id,
            },
        }
        mission.finished_at = now_ts()
        self.store.save(mission)

        self.store.record_event(
            mission.id,
            "mission.recovered",
            attempt=mission.attempt,
            data={"recovered_by": self.id},
        )
