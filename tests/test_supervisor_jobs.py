"""Worker N: durable job model, leases, heartbeats, checkpoints."""

from app.supervisor.models import JobState, Stage
from app.supervisor.repository import SupervisorRepository
from app.supervisor.supervisor import LeaseLost, Supervisor


def make_supervisor(tmp_path, worker_id="w1", **kwargs):
    repo = SupervisorRepository(tmp_path / f"{worker_id}.sqlite")
    return Supervisor(repo=repo, worker_id=worker_id, **kwargs), repo


def test_create_job_tracks_required_fields(tmp_path):
    sup, _ = make_supervisor(tmp_path)
    job = sup.create_job(mission_id="m1")
    assert job.job_id
    assert job.mission_id == "m1"
    assert job.state == JobState.queued
    assert job.created_at
    assert job.updated_at
    assert job.attempt == 0
    assert job.lease_owner is None


def test_claim_assigns_bounded_lease(tmp_path):
    sup, _ = make_supervisor(tmp_path, lease_seconds=60)
    job = sup.create_job()
    claimed = sup.claim(job.job_id)
    assert claimed is not None
    assert claimed.state == JobState.claimed
    assert claimed.worker_id == "w1"
    assert claimed.lease_owner == "w1"
    assert claimed.lease_expiry
    assert claimed.lease_expiry > claimed.heartbeat_at
    assert claimed.attempt == 1
    assert claimed.started_at


def test_second_worker_cannot_claim_valid_lease(tmp_path):
    sup1, repo = make_supervisor(tmp_path, worker_id="w1")
    sup2 = Supervisor(repo=repo, worker_id="w2")
    job = sup1.create_job()
    assert sup1.claim(job.job_id) is not None
    assert sup2.claim(job.job_id) is None


def test_heartbeat_extends_lease_only_for_owner(tmp_path):
    sup1, repo = make_supervisor(tmp_path, worker_id="w1")
    sup2 = Supervisor(repo=repo, worker_id="w2")
    job = sup1.create_job()
    sup1.claim(job.job_id)
    before = sup1.get(job.job_id).lease_expiry
    assert sup1.heartbeat(job.job_id) is True
    assert sup1.get(job.job_id).lease_expiry >= before
    assert sup2.heartbeat(job.job_id) is False


def test_stale_owner_cannot_finalize_after_losing_lease(tmp_path):
    import time

    sup1, repo = make_supervisor(
        tmp_path, worker_id="w1", lease_seconds=1
    )
    sup2 = Supervisor(repo=repo, worker_id="w2", lease_seconds=60)
    job = sup1.create_job()
    sup1.claim(job.job_id)
    time.sleep(1.1)
    assert sup2.claim(job.job_id) is not None
    try:
        sup1.pass_job(job.job_id)
    except LeaseLost:
        pass
    else:
        raise AssertionError("stale owner finalized the job")
    assert sup2.pass_job(job.job_id).state == JobState.passed


def test_checkpoint_persists_stage_metadata(tmp_path):
    sup, repo = make_supervisor(tmp_path)
    job = sup.create_job()
    sup.claim(job.job_id)
    checkpoint = sup.checkpoint(
        job.job_id,
        Stage.execute_step,
        step=2,
        evidence_refs=["ev-1"],
        committed_actions=["write-file"],
        summary="wrote output",
    )
    assert checkpoint.stage == Stage.execute_step
    assert checkpoint.step == 2
    latest = repo.latest_checkpoint(job.job_id)
    assert latest.stage == Stage.execute_step
    assert latest.committed_actions == ["write-file"]
    stored = sup.get(job.job_id)
    assert stored.stage == Stage.execute_step
    assert stored.step == 2
    assert "ev-1" in stored.evidence_refs


def test_complete_step_skips_already_committed_action(tmp_path):
    sup, _ = make_supervisor(tmp_path)
    job = sup.create_job()
    sup.claim(job.job_id)
    first = sup.complete_step(
        job.job_id, "deploy", Stage.execute_step, step=1,
        evidence_ref="ev-deploy",
    )
    assert first is not None
    assert sup.complete_step(
        job.job_id, "deploy", Stage.execute_step, step=1,
        evidence_ref="ev-deploy-again",
    ) is None
    assert len(sup.repo.checkpoints(job.job_id)) == 1


def test_backoff_is_bounded_and_retry_reason_persisted(tmp_path):
    sup, _ = make_supervisor(tmp_path, max_retries=2)
    job = sup.create_job()
    sup.claim(job.job_id)
    retrying = sup.fail_job(
        job.job_id, "ProviderError", "timeout",
        retryable=True, reason="provider timeout",
    )
    assert retrying.state == JobState.queued
    assert retrying.retry_reason == "provider timeout"
    assert retrying.next_retry_at
    assert retrying.retries == 1
    sup.claim(job.job_id)
    retrying = sup.fail_job(
        job.job_id, "ProviderError", "timeout",
        retryable=True, reason="provider timeout",
    )
    assert retrying.retries == 2
    sup.claim(job.job_id)
    terminal = sup.fail_job(
        job.job_id, "ProviderError", "timeout",
        retryable=True, reason="provider timeout",
    )
    assert terminal.state == JobState.failed
    assert terminal.finished_at


def test_provider_outage_parks_job_and_resumes(tmp_path):
    sup, _ = make_supervisor(tmp_path)
    job = sup.create_job()
    sup.claim(job.job_id)
    sup.checkpoint(job.job_id, Stage.plan, step=1)
    blocked = sup.block_on_provider(job.job_id, reason="provider down")
    assert blocked.state == JobState.blocked_external
    assert sup.repo.latest_checkpoint(job.job_id) is not None
    resumed = sup.unblock_provider(job.job_id)
    assert resumed is not None
    assert resumed.state == JobState.claimed


def test_events_emitted_without_secrets(tmp_path):
    sup, _ = make_supervisor(tmp_path)
    job = sup.create_job()
    sup.claim(job.job_id)
    sup.heartbeat(job.job_id)
    sup.checkpoint(job.job_id, Stage.observe, step=0)
    sup.pass_job(job.job_id)
    events = sup.repo.events(job.job_id)
    kinds = [e.event_type for e in events]
    for expected in (
        "JOB_CREATED", "CLAIMED", "HEARTBEAT", "CHECKPOINT", "PASSED",
    ):
        assert expected in kinds
    blob = " ".join(
        f"{e.event_type}{e.data}" for e in events
    ).lower()
    assert "secret" not in blob
    assert "token" not in blob
    assert "api_key" not in blob
