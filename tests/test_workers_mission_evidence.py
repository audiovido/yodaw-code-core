"""Focused tests for mission evidence: deterministic structure, bounded
output, and the PASS/FAIL-compatible summary schema."""

import json

from app.workers.mission_evidence import (
    MAX_EVIDENCE_EVENTS,
    accumulate,
    clamp_event,
    make_event,
    pass_fail_summary,
    provider_attempt_evidence,
)


def test_event_has_deterministic_structure():
    event = make_event("validator.started", timestamp="2026-01-01T00:00:00+00:00", files=["a"])
    assert list(event) == ["type", "timestamp", "files"]
    twin = make_event("validator.started", timestamp="2026-01-01T00:00:00+00:00", files=["a"])
    assert json.dumps(event) == json.dumps(twin)


def test_event_timestamp_defaults_to_now():
    event = make_event("x")
    assert event["type"] == "x"
    assert event["timestamp"]
    assert event["timestamp"].endswith("+00:00") or "T" in event["timestamp"]


def test_clamp_event_is_bounded():
    event = make_event("plan", timestamp="t", payload="x" * 200_000)
    clamped = clamp_event(event, max_bytes=2048)
    assert len(json.dumps(clamped)) <= 4096
    assert clamped["type"] == "plan"
    assert clamped["timestamp"] == "t"
    assert clamped.get("truncated") is True
    assert clamped["truncated_bytes"] > 0


def test_small_events_not_clamped():
    event = make_event("small", timestamp="t", n=1)
    assert clamp_event(event) is event
    assert event.get("truncated") is None


def test_accumulate_caps_length():
    evidence = []
    for index in range(MAX_EVIDENCE_EVENTS + 50):
        accumulate(evidence, make_event("e%d" % index, timestamp="t"))
    assert len(evidence) <= MAX_EVIDENCE_EVENTS + 1
    assert any(event["type"] == "evidence_truncated" for event in evidence)


def test_pass_fail_summary_all_pass():
    results = [
        {"name": "pytest", "returncode": 0, "timed_out": False, "stderr": ""},
        {"name": "npm test", "returncode": 0, "timed_out": False, "stderr": ""},
    ]
    summary = pass_fail_summary(results)
    assert summary["status"] == "PASS"
    assert summary["passed"] == 2
    assert summary["failed"] == 0
    assert summary["total"] == 2


def test_pass_fail_summary_failure():
    results = [
        {"name": "pytest", "returncode": 1, "timed_out": False, "stderr": "boom"},
        {"name": "go test", "returncode": 0, "timed_out": False, "stderr": ""},
    ]
    summary = pass_fail_summary(results)
    assert summary["status"] == "FAIL"
    assert summary["failed"] == 1
    assert summary["checks"][0]["detail"] == "boom"


def test_pass_fail_summary_timeout_is_failure():
    results = [{"name": "slow", "returncode": -9, "timed_out": True, "stderr": ""}]
    assert pass_fail_summary(results)["status"] == "FAIL"


def test_pass_fail_summary_empty_is_pass():
    summary = pass_fail_summary([])
    assert summary["status"] == "PASS"
    assert summary["total"] == 0


def test_pass_fail_summary_required_conditions():
    summary = pass_fail_summary([], required_conditions={"diff_guard": True})
    assert summary["status"] == "PASS"
    assert summary["checks"][0]["name"] == "diff_guard"


def test_pass_fail_summary_schema_stable():
    results = [{"name": "t", "returncode": 0, "timed_out": False, "stderr": ""}]
    first = json.dumps(pass_fail_summary(results), sort_keys=True)
    second = json.dumps(pass_fail_summary(results), sort_keys=True)
    assert first == second


def test_provider_attempt_evidence_shape():
    result = provider_attempt_evidence()
    assert isinstance(result, list)
    for event in result:
        assert event["type"] == "provider_attempts"
        assert "attempts" in event