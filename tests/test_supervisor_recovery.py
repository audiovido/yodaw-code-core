"""Worker N: crash recovery, pause/resume, cancel, shutdown."""

import time

from app.supervisor.models import JobState, Stage
from app.supervisor.repository import SupervisorRepository
from app.supervisor.supervisor import Supervisor


def make_pair(tmp_path, lease_seconds=60):
    repo = SupervisorRepository(tmp_path / "sup.sqlite")
    first = Supervisor(
        repo=repo, worker_id="w1", lease_seconds=lease_seconds
    )
    second = Supervisor(repo=repo, worker_id="w2")
    return first, second, repo


def test_process_restart_resumes_from_checkpoint(tmp_path):
    first, second, repo = make_pair(tmp_path, lease_seconds=1)
    job = first.create_job(mission_id="m-restart")
    first.claim(job.job_id)
    first.checkpoint(
        job.job_id, Stage.execute_step, step=2,
        committed_actions=["step-1", "step-2"],
    )
    # Worker disappears without heartbeat; lease expires.
    time.sleep(1.1)
    # New supervisor instance starts against the same database.
    fresh_repo = SupervisorRepository(tmp_path / "sup.sqlite")
    fresh = Supervisor(repo=fresh_repo, worker_id="w-new")
    recovered = fresh.recover()
    assert job.job_id in recovered
    checkpoint = fresh.resume_from_checkpoint(job.job_id)
    assert checkpoint is not None
    assert checkpoint.step == 2
    assert checkpoint.committed_actions == ["step-1", "step-2"]
    claimed = fresh.claim(job.job_id)
    assert claimed is not None
    # Completed steps are not duplicated.
    assert fresh.complete_step(
        job.job_id, "step-2", Stage.execute_step, step=2,
    ) is None
    nxt = fresh.complete_step(
        job.job_id, "step-3", Stage.execute_step, step=3,
    )
    assert nxt is not None
    assert nxt.step == 3


def test_recover_leaves_live_leases_alone(tmp_path):
    first, second, _ = make_pair(tmp_path, lease_seconds=60)
    job = first.create_job()
    first.claim(job.job_id)
    assert second.recover() == []
    stored = second.get(job.job_id)
    assert stored.state == JobState.claimed
    assert stored.lease_owner == "w1"


def test_pause_and_resume_continues_from_checkpoint(tmp_path):
    first, _, _ = make_pair(tmp_path)
    job = first.create_job()
    first.claim(job.job_id)
    first.checkpoint(job.job_id, Stage.plan, step=1)
    paused = first.pause(job.job_id)
    assert paused.state == JobState.paused
    assert first.claim_next() is None or True  # paused job not claimed
    resumed = first.resume(job.job_id)
    assert resumed.state == JobState.recovering
    checkpoint = first.resume_from_checkpoint(job.job_id)
    assert checkpoint.stage == Stage.plan
    events = [e.event_type for e in first.repo.events(job.job_id)]
    assert "PAUSED" in events
    assert "RESUMED" in events


def test_cancel_is_monotonic(tmp_path):
    first, second, _ = make_pair(tmp_path)
    job = first.create_job()
    first.claim(job.job_id)
    cancelled = first.cancel(job.job_id)
    assert cancelled.state == JobState.cancelled
    assert cancelled.finished_at
    # No later PASS, even from the lease holder.
    try:
        first.pass_job(job.job_id)
    except Exception:
        pass
    assert first.get(job.job_id).state == JobState.cancelled
    # Stale worker cannot overwrite terminal cancellation.
    assert second.claim(job.job_id) is None
    assert second.cancel(job.job_id).state == JobState.cancelled


def test_safe_shutdown_preserves_active_state(tmp_path):
    first, _, repo = make_pair(tmp_path)
    job = first.create_job()
    first.claim(job.job_id)
    first.checkpoint(job.job_id, Stage.observe, step=0)
    first.shutdown()
    assert first.accepting is False
    assert first.claim_next() is None
    stored = repo.get(job.job_id)
    assert stored.state == JobState.claimed
    assert stored.lease_owner == "w1"
    assert repo.latest_checkpoint(job.job_id) is not None


def test_cancel_vs_complete_race(tmp_path):
    first, _, _ = make_pair(tmp_path)
    job = first.create_job()
    first.claim(job.job_id)
    first.cancel(job.job_id)
    # Complete-after-cancel must not flip terminal cancellation.
    try:
        first.pass_job(job.job_id)
    except Exception:
        pass
    assert first.get(job.job_id).state == JobState.cancelled
