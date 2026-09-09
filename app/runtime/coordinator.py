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

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from app.core.models import Mission, MissionStatus, TERMINAL_STATUSES
from app.storage.sqlite_store import MissionStore
from app.runtime.repo_leases import RepoLeaseManager, now_ts


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
        store: MissionStore | None = None,
        leases: RepoLeaseManager | None = None,
        registry=None,
        id_prefix: str = "coord",
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
        self._thread: threading.Thread | None = None
        self._shutting_down = False

    # -----------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------

    def start(self):
        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(
            target=self._run_loop,
            name=self.id,
            daemon=True,
        )
        self._thread.start()

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
        import time

        last_heartbeat = 0.0
        last_watchdog = 0.0

        while not self._stop.is_set():
            now = time.monotonic()

            if now - last_heartbeat >= self.heartbeat_interval / 2:
                self._heartbeat_inflight()
                last_heartbeat = now

            if now - last_watchdog >= self.heartbeat_interval:
                try:
                    self.recover_stale_missions()
                except Exception:
                    pass
                last_watchdog = now

            claimed = self._claim_and_dispatch()

            if not claimed:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
            else:
                time.sleep(0.05)

    def _heartbeat_inflight(self):
        with self._inflight_lock:
            items = list(self._inflight)

        for mission_id in items:
            try:
                self.store.heartbeat(mission_id, self.id)
            except Exception:
                pass

        for repo_key in self.leases.held_by(self.id):
            try:
                self.leases.heartbeat(repo_key, self.id)
            except Exception:
                pass

    def _claim_and_dispatch(self) -> bool:
        with self._inflight_lock:
            capacity = self.max_concurrent - len(self._inflight)
            held = set(self._inflight_repos)

        if capacity <= 0 or self._shutting_down:
            return False

        mission = self.store.claim_next(
            self.id,
            skip_repo_keys=held,
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
        try:
            mission.status = MissionStatus.queued
            mission.claimed_by = None
            mission.claimed_at = None
            mission.heartbeat_at = None
            self.store.save(mission)
        except Exception:
            pass

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
                store.save(fresh)
                return

            fresh.worker = worker.name

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
                fresh.status = MissionStatus.failed
                fresh.result = {
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                }
                fresh.finished_at = now_ts()
                store.save(fresh)
                store.record_event(
                    fresh.id,
                    "mission.completed",
                    attempt=fresh.attempt,
                    data={"outcome": "FAIL", "error": str(exc)},
                )
                return

            # Cancellation observed mid-flight by the worker?
            error_type = (result.get("error") or {}).get("type", "")

            if error_type == "Cancelled":
                fresh.status = MissionStatus.cancelled
                fresh.result = dict(result.get("output", {}))
                fresh.result["error"] = result["error"]
                fresh.evidence = list(result.get("evidence", []))
                fresh.finished_at = now_ts()
                store.save(fresh)
                return

            fresh.evidence = list(result.get("evidence", []))
            fresh.result = dict(result.get("output", {}))

            if result.get("error"):
                fresh.result["error"] = result["error"]

            fresh.status = (
                MissionStatus.passed
                if result.get("success")
                else MissionStatus.failed
            )
            fresh.finished_at = now_ts()

            store.save(fresh)

            store.record_event(
                fresh.id,
                "mission.completed",
                attempt=fresh.attempt,
                data={
                    "outcome": fresh.status.value,
                    "commit_sha": fresh.result.get("commit_sha"),
                },
            )

            # Stage 7 learning behavior preserved.
            from app.learning.engine import learn_from_result

            learn_from_result(
                mission_id=fresh.id,
                goal=fresh.goal,
                worker=fresh.worker,
                success=result.get("success", False),
                evidence=fresh.evidence,
                result=fresh.result,
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
