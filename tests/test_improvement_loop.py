"""Hermetic tests for the governed self-improving loop."""
import sqlite3

import pytest

from app.eval.models import (
    EvaluationResult,
    FailureMode,
    PerformanceMetrics,
    ResultClass,
)
from app.improvement.approval import approve, reject, submit_for_review
from app.improvement.audit import LoopAudit
from app.improvement.candidates import (
    MAX_ACCEPTABLE_RISK,
    find_unsafe_markers,
    generate_candidates,
    risk_for_modes,
)
from app.improvement.loop import SelfImprovingLoop
from app.improvement.models import ProposalStatus, is_terminal
from app.improvement.recovery import recover_history
from app.improvement.store import ProposalStore
from app.improvement.validation import (
    RegressionScores,
    SuiteScores,
    check_regression,
    validate_proposal,
)
from app.improvement.versioning import ActivationStore
from app.learning.clusters import FailureCluster, cluster_signals
from app.learning.failure_miner import mine_failures


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "loop_test.db"


@pytest.fixture()
def stores(db_path):
    proposals = ProposalStore(path=db_path)
    activations = ActivationStore(path=db_path, proposals=proposals)
    audit = LoopAudit(path=db_path)
    return proposals, activations, audit


def _perf() -> PerformanceMetrics:
    return PerformanceMetrics(
        wall_clock_time=1.0, retries=0, files_changed=1, diff_lines=10
    )


def _fail(case_id: str) -> EvaluationResult:
    return EvaluationResult(
        case_id=case_id,
        result_class=ResultClass.FAIL_CORRECTNESS,
        score=50.0,
        correctness_score=10.0,
        regression_safety_score=10.0,
        change_minimality_score=5.0,
        test_quality_score=5.0,
        evidence_quality_score=5.0,
        efficiency_score=5.0,
        reasons=["test"],
        evidence={"mission_id": case_id},
        performance=_perf(),
        failure_modes=[FailureMode.VALIDATION_ERROR],
    )


def _cluster(cluster_id: str = "fc_test") -> FailureCluster:
    return FailureCluster(
        id=cluster_id,
        signature="FAIL_CORRECTNESS:validation_error",
        result_class="FAIL_CORRECTNESS",
        failure_modes=["validation_error"],
        size=3,
        case_ids=["a", "b", "c"],
        avg_score=50.0,
    )


def _approved_proposal(stores):
    proposals, _, _ = stores
    proposal, created = proposals.create(
        kind="SKILL",
        title="t",
        description="d",
        content={"guidance": "safe text"},
        content_hash="hash-approved-1",
    )
    assert created
    submit_for_review(proposals, proposal.id)
    return approve(proposals, proposal.id, reviewer="reviewer")


# --- proposal creation -------------------------------------------


def test_proposal_creation(stores):
    proposals, _, audit = stores
    cluster = _cluster()
    candidates, rejections = generate_candidates(cluster)
    assert not rejections
    assert candidates
    candidate = candidates[0]
    assert candidate.expected_impact
    assert candidate.risk_score >= 0
    assert candidate.validation_plan
    proposal, created = proposals.create(
        kind=candidate.kind,
        title=candidate.title,
        description=candidate.description,
        cluster_id=cluster.id,
        expected_impact=candidate.expected_impact,
        risk_score=candidate.risk_score,
        validation_plan=candidate.validation_plan,
        content=candidate.content,
        content_hash=candidate.content_hash,
    )
    assert created
    fetched = proposals.get(proposal.id)
    assert fetched is not None
    assert fetched.status == ProposalStatus.DRAFT
    assert fetched.benchmark is None
    assert fetched.regression is None


def test_duplicate_suppression(stores):
    proposals, _, _ = stores
    first, created_first = proposals.create(
        kind="SKILL", title="t", content={"a": 1}, content_hash="dup-hash"
    )
    second, created_second = proposals.create(
        kind="SKILL", title="t copy", content={"a": 1}, content_hash="dup-hash"
    )
    assert created_first
    assert not created_second
    assert second.id == first.id
    assert proposals.count() == 1


def test_unsafe_proposal_rejection():
    bad = {"guidance": "use auto_approve to bypass validation"}
    assert find_unsafe_markers(bad)
    assert risk_for_modes(["test_regression"]) <= MAX_ACCEPTABLE_RISK


def test_over_risk_candidate_refused(monkeypatch):
    import app.improvement.candidates as cand

    monkeypatch.setattr(cand, "MAX_ACCEPTABLE_RISK", -1.0)
    candidates, reasons = generate_candidates(_cluster())
    assert candidates == []
    assert reasons


# --- validation / regression --------------------------------------


def test_validation_failure(stores):
    proposals, _, _ = stores
    proposal, _ = proposals.create(
        kind="SKILL", title="t", content={}, content_hash="val-fail"
    )
    outcome = validate_proposal(
        proposal,
        SuiteScores(suite="s", before_score=80.0, after_score=70.0, cases_run=3),
    )
    assert not outcome.passed
    proposals.save(proposal)
    assert proposal.benchmark is not None
    assert proposal.benchmark.improvement < 0


def test_regression_degradation_rejection(stores):
    proposals, _, _ = stores
    proposal, _ = proposals.create(
        kind="SKILL", title="t", content={}, content_hash="reg-fail"
    )
    outcome = check_regression(
        proposal,
        RegressionScores(
            suite="s", failures_before=0, failures_after=2, passed=True
        ),
    )
    assert not outcome.passed
    proposals.save(proposal)
    assert proposal.regression is not None
    assert proposal.regression.delta == 2


# --- approval gate -------------------------------------------------


def test_approval_required(stores):
    proposals, activations, _ = stores
    proposal, _ = proposals.create(
        kind="SKILL", title="t", content={}, content_hash="gate-1"
    )
    with pytest.raises(ValueError):
        activations.activate(proposal.id)
    assert proposals.get(proposal.id).status == ProposalStatus.DRAFT


def test_activation_after_approval(stores):
    proposals, activations, _ = stores
    proposal = _approved_proposal(stores)
    record = activations.activate(proposal.id, actor="reviewer")
    assert record.version == 1
    assert record.proposal_id == proposal.id
    assert activations.current() is not None
    assert proposals.get(proposal.id).status == ProposalStatus.ACTIVATED


def test_rejected_proposal_retained(stores):
    proposals, _, _ = stores
    proposal, _ = proposals.create(
        kind="SKILL", title="t", content={}, content_hash="rej-1"
    )
    submit_for_review(proposals, proposal.id)
    rejected = reject(proposals, proposal.id, reviewer="r", reason="no evidence")
    assert rejected.status == ProposalStatus.REJECTED
    assert is_terminal(rejected.status)
    assert proposals.get(proposal.id).rejection_reason == "no evidence"


# --- rollback / lineage --------------------------------------------


def test_rollback(stores):
    proposals, activations, _ = stores
    first = _approved_proposal(stores)
    activations.activate(first.id, actor="reviewer")
    second_data = proposals.create(
        kind="SKILL", title="t2", content={"b": 2}, content_hash="rollback-2"
    )[0]
    submit_for_review(proposals, second_data.id)
    approve(proposals, second_data.id, reviewer="reviewer")
    activations.activate(second_data.id, actor="reviewer")
    restored = activations.rollback(actor="reviewer", reason="bad")
    assert restored.version == 1
    assert proposals.get(second_data.id).status == ProposalStatus.ROLLED_BACK


def test_version_lineage(stores):
    proposals, activations, _ = stores
    first = _approved_proposal(stores)
    activations.activate(first.id, actor="reviewer")
    history = activations.history()
    assert [r.version for r in history] == [1]
    assert history[0].previous_version is None


# --- audit / silent mutation ---------------------------------------


def test_audit_evidence(stores):
    proposals, activations, audit = stores
    proposal = _approved_proposal(stores)
    activations.activate(proposal.id, actor="reviewer")
    audit.record("loop.activated", {"proposal_id": proposal.id})
    events = audit.events("loop.activated")
    assert len(events) == 1
    assert audit.verify()["intact"]


def test_no_silent_mutation(stores):
    proposals, activations, audit = stores
    proposal = _approved_proposal(stores)
    audit.record("loop.approved", {"proposal_id": proposal.id})
    with pytest.raises(ValueError):
        proposals.transition(proposal.id, ProposalStatus.ACTIVATED)
    activations.activate(proposal.id, actor="reviewer")
    events = audit.events()
    actions = {e["action"] for e in events}
    assert "loop.approved" in actions


def test_corrupted_history_recovery(tmp_path):
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"not a database at all")
    report = recover_history(bad)
    assert report.quarantined_to is not None
    proposals = ProposalStore(path=bad)
    assert proposals.count() == 0
    with sqlite3.connect(bad) as db:
        tables = {
            r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "improvement_proposals" in tables


# --- bounded loop ---------------------------------------------------


def test_bounded_iteration_loop(db_path):
    loop = SelfImprovingLoop(path=db_path, min_occurrences=3)
    batch = [_fail(f"case-{i}") for i in range(3)]
    report = loop.run([batch, batch, batch], )
    assert 1 <= report.ran_iterations <= 5
    first = report.iterations[0]
    assert first.clusters == 1
    assert first.proposals_created >= 1
    assert first.submitted >= 1


def test_loop_stops_without_recurrence(db_path):
    loop = SelfImprovingLoop(path=db_path, min_occurrences=3)
    report = loop.run_iteration([_fail("lonely")], iteration=1)
    assert report.stopped
    assert report.stop_reason == "no recurring clusters"


def test_unique_failure_ignored_end_to_end(db_path):
    loop = SelfImprovingLoop(path=db_path, min_occurrences=3)
    signals, _ = mine_failures([_fail("solo")])
    clusters, ignored = cluster_signals(signals, min_occurrences=3)
    assert clusters == []
    assert len(ignored) == 1
    report = loop.run_iteration([_fail("solo")], iteration=1)
    assert report.proposals_created == 0
