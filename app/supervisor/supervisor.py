"""Durable long-running task supervisor.

Coordinates durable jobs: bounded worker leases with heartbeats,
stage checkpoints, crash recovery from persisted state, pause and
resume, monotonic cancellation, bounded retry/backoff, provider
outage parking, idempotent side-effect guards, structured events,
and safe shutdown.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from app.supervisor.models import (
    TERMINAL_STATES,
    Checkpoint,
    JobRecord,
    JobState,
    Stage,
    now_iso,
)
from app.supervisor.repository import SupervisorRepository

logger = logging.getLogger("yodaw.supervisor")

LEASE_SECONDS_DEFAULT = 60
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0

STAGES = (
    Stage.observe,
    Stage.plan,
    Stage.execute_step,
    Stage.verify,
    Stage.recover,
)


def backoff_delay_seconds(
    retries: int,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_MAX_SECONDS,
) -> float:
    delay = base * (2.0 ** max(0, retries))
    return min(cap, delay)


class LeaseLost(Exception):
    """The worker no longer owns the job lease."""


class Supervisor:
    """Owns durable job execution for one worker identity."""

    def __init__(
        self,
        repo: SupervisorRepository | None = None,
        worker_id: str | None = None,
        lease_seconds: int = LEASE_SECONDS_DEFAULT,
        max_retries: int = 3,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        backoff_max: float = BACKOFF_MAX_SECONDS,
    ):
        import uuid

        self.repo = repo or SupervisorRepository()
        self.worker_id = worker_id or f"sup_{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self._lock = threading.Lock()
        self._accepting = True
        self._closed = False

    # ------------------------------------------------- lifecycle
    def create_job(
        self,
        mission_id: str | None = None,
        max_retries: int | None = None,
    ) -> JobRecord:
        job = JobRecord(
            mission_id=mission_id,
            max_retries=(
                self.max_retries if max_retries is None else max_retries
            ),
        )
        self.repo.create(job)
        self.repo.record_event(
            job.job_id, "JOB_CREATED", data={"mission_id": mission_id}
        )
        return job

    def get(self, job_id: str) -> JobRecord | None:
        return self.repo.get(job_id)

    def shutdown(self) -> None:
        """Stop accepting new claims; preserve active state."""
        with self._lock:
            self._accepting = False
            self._closed = True

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting and not self._closed

    # ------------------------------------------------- claim/lease
    def claim(self, job_id: str) -> JobRecord | None:
        if not self.accepting:
            return None
        job = self.repo.claim(job_id, self.worker_id, self.lease_seconds)
        if job is None:
            return None
        self.repo.record_event(
            job.job_id,
            "CLAIMED",
            attempt=job.attempt,
            data={"worker_id": self.worker_id},
        )
        return job

    def claim_next(self) -> JobRecord | None:
        if not self.accepting:
            return None
        job = self.repo.claim_next(self.worker_id, self.lease_seconds)
        if job is None:
            return None
        self.repo.record_event(
            job.job_id,
            "CLAIMED",
            attempt=job.attempt,
            data={"worker_id": self.worker_id},
        )
        return job

    def heartbeat(self, job_id: str) -> bool:
        ok = self.repo.heartbeat(job_id, self.worker_id, self.lease_seconds)
        if ok:
            job = self.repo.get(job_id)
            self.repo.record_event(
                job_id,
                "HEARTBEAT",
                attempt=job.attempt if job else 0,
                data={"worker_id": self.worker_id},
            )
        return ok

    def _require_lease(self, job_id: str) -> JobRecord:
        job = self.repo.get(job_id)
        if job is None:
            raise LeaseLost(f"unknown job {job_id}")
        if job.state in TERMINAL_STATES:
            raise LeaseLost(f"job {job_id} is terminal")
        if job.lease_owner != self.worker_id:
            raise LeaseLost(f"lease for job {job_id} not owned")
        now_s = now_iso()
        if job.lease_expiry and job.lease_expiry < now_s:
            raise LeaseLost(f"lease for job {job_id} expired")
        return job

    # ------------------------------------------------- step loop
    def mark_running(self, job_id: str, stage: Stage) -> JobRecord:
        job = self._require_lease(job_id)
        if job.pause_requested or job.state == JobState.paused:
            return job
        job.state = JobState.running
        job.stage = stage
        self.repo.save(job)
        return job

    def already_committed(self, job_id: str, action_key: str) -> bool:
        checkpoint = self.repo.latest_checkpoint(job_id)
        if checkpoint is None:
            return False
        return action_key in checkpoint.committed_actions

    def checkpoint(
        self,
        job_id: str,
        stage: Stage,
        step: int = 0,
        evidence_refs: list[str] | None = None,
        committed_actions: list[str] | None = None,
        summary: str = "",
    ) -> Checkpoint:
        job = self._require_lease(job_id)
        checkpoint = Checkpoint(
            job_id=job_id,
            stage=stage,
            step=step,
            attempt=job.attempt,
            evidence_refs=list(evidence_refs or []),
            committed_actions=list(committed_actions or []),
            summary=summary,
        )
        self.repo.save_checkpoint(checkpoint)
        job.stage = stage
        job.step = step
        for ref in checkpoint.evidence_refs:
            if ref not in job.evidence_refs:
                job.evidence_refs.append(ref)
        self.repo.save(job)
        self.repo.record_event(
            job_id,
            "CHECKPOINT",
            attempt=job.attempt,
            data={"stage": stage.value, "step": step},
        )
        return checkpoint

    def complete_step(
        self,
        job_id: str,
        action_key: str,
        stage: Stage,
        step: int,
        evidence_ref: str | None = None,
    ) -> Checkpoint | None:
        """Record one idempotent side-effect; skip when committed.

        Returns the new checkpoint, or None when the action was
        already committed in a prior checkpoint.
        """
        if self.already_committed(job_id, action_key):
            return None
        refs = [evidence_ref] if evidence_ref else []
        return self.checkpoint(
            job_id,
            stage,
            step=step,
            evidence_refs=refs,
            committed_actions=[action_key],
        )

    # ------------------------------------------------- terminal
    def pass_job(self, job_id: str) -> JobRecord:
        job = self._require_lease(job_id)
        if job.cancel_requested or job.state == JobState.cancelled:
            return job
        job.state = JobState.passed
        job.finished_at = now_iso()
        job.lease_owner = None
        job.lease_expiry = None
        self.repo.save(job)
        self.repo.record_event(
            job_id, "PASSED", attempt=job.attempt, data={}
        )
        return job

    def fail_job(
        self,
        job_id: str,
        error_type: str,
        message: str = "",
        retryable: bool = False,
        reason: str = "",
    ) -> JobRecord:
        job = self._require_lease(job_id)
        if job.state in TERMINAL_STATES:
            return job
        job.last_error_type = error_type
        job.last_error_message = message
        if retryable and job.retries < job.max_retries:
            job.retries += 1
            job.retry_reason = reason or error_type
            delay = backoff_delay_seconds(
                job.retries - 1, self.backoff_base, self.backoff_max
            )
            job.next_retry_at = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat()
            job.state = JobState.queued
            job.lease_owner = None
            job.lease_expiry = None
            self.repo.save(job)
            self.repo.record_event(
                job_id,
                "RETRY",
                attempt=job.attempt,
                data={"reason": job.retry_reason, "delay": delay},
            )
            return job
        job.state = JobState.failed
        job.finished_at = now_iso()
        job.lease_owner = None
        job.lease_expiry = None
        self.repo.save(job)
        self.repo.record_event(
            job_id,
            "FAILED",
            attempt=job.attempt,
            data={"error_type": error_type},
        )
        return job

    def block_on_provider(self, job_id: str, reason: str = "") -> JobRecord:
        """Park the job on provider outage; state is retained."""
        job = self._require_lease(job_id)
        job.state = JobState.blocked_external
        job.retry_reason = reason
        self.repo.save(job)
        return job

    def unblock_provider(self, job_id: str) -> JobRecord | None:
        job = self.repo.get(job_id)
        if job is None or job.state != JobState.blocked_external:
            return job
        return self.claim(job_id)

    # ------------------------------------------------- pause/resume
    def pause(self, job_id: str) -> JobRecord | None:
        job = self.repo.get(job_id)
        if job is None or job.state in TERMINAL_STATES:
            return job
        job.pause_requested = True
        if job.state in (JobState.queued, JobState.claimed):
            job.state = JobState.paused
        elif job.state == JobState.running:
            job.state = JobState.paused
        self.repo.save(job)
        self.repo.record_event(
            job_id, "PAUSED", attempt=job.attempt, data={}
        )
        return job

    def resume(self, job_id: str) -> JobRecord | None:
        job = self.repo.get(job_id)
        if job is None:
            return None
        if job.state in TERMINAL_STATES:
            return job
        job.pause_requested = False
        job.state = JobState.recovering
        job.next_retry_at = None
        self.repo.save(job)
        self.repo.record_event(
            job_id, "RESUMED", attempt=job.attempt, data={}
        )
        return job

    # ------------------------------------------------- cancel
    def cancel(self, job_id: str) -> JobRecord | None:
        """Monotonic cancellation: terminal, never overwritten."""
        job = self.repo.get(job_id)
        if job is None:
            return None
        if job.state in TERMINAL_STATES:
            return job
        job.cancel_requested = True
        job.state = JobState.cancelled
        job.finished_at = now_iso()
        job.lease_owner = None
        job.lease_expiry = None
        self.repo.save(job)
        self.repo.record_event(
            job_id, "CANCELLED", attempt=job.attempt, data={}
        )
        return job

    # ------------------------------------------------- recovery
    def recover(self) -> list[str]:
        """Discover non-terminal jobs; recover stale leases.

        Jobs with a live lease are left alone. Jobs with an
        expired (or missing) lease move to RECOVERING so a new
        worker can resume them from their latest checkpoint.
        """
        recovered: list[str] = []
        now_s = now_iso()
        for job in self.repo.non_terminal():
            live = (
                job.lease_owner
                and job.lease_expiry
                and job.lease_expiry >= now_s
                and job.state in (JobState.claimed, JobState.running)
            )
            if live:
                continue
            if job.state in TERMINAL_STATES:
                continue
            if job.state == JobState.paused and job.pause_requested:
                continue
            if job.state == JobState.blocked_external:
                continue
            if job.next_retry_at and job.next_retry_at > now_s:
                continue
            job.state = JobState.recovering
            job.lease_owner = None
            job.lease_expiry = None
            self.repo.save(job)
            self.repo.record_event(
                job.job_id,
                "RECOVERED",
                attempt=job.attempt,
                data={"from_state": job.state.value},
            )
            recovered.append(job.job_id)
        return recovered

    def resume_from_checkpoint(self, job_id: str) -> Checkpoint | None:
        """Latest checkpoint to resume from; None when fresh."""
        return self.repo.latest_checkpoint(job_id)

    def lease_expired(self, job_id: str) -> bool:
        job = self.repo.get(job_id)
        if job is None or not job.lease_expiry:
            return True
        return job.lease_expiry < now_iso()
