"""Focused tests for the shared worker error types."""

import pytest

from app.workers.worker_errors import (
    MissionCancelled,
    SubprocessTimeout,
    ToolMissingError,
)


def test_mission_cancelled_carries_checkpoint():
    exc = MissionCancelled("before_commit")
    assert exc.at == "before_commit"
    assert "before_commit" in str(exc)


def test_mission_cancelled_default_checkpoint():
    exc = MissionCancelled()
    assert exc.at == ""


def test_tool_missing_is_runtime_error():
    exc = ToolMissingError("npm missing")
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "npm missing"


def test_subprocess_timeout_is_timeout_error():
    exc = SubprocessTimeout("deadline exceeded")
    assert isinstance(exc, TimeoutError)


def test_error_types_are_distinct():
    assert MissionCancelled.__mro__[1] is Exception
    assert ToolMissingError.__mro__[1] is RuntimeError
    assert SubprocessTimeout.__mro__[1] is TimeoutError


def test_tool_missing_not_swallowed_as_failure():
    with pytest.raises(ToolMissingError):
        raise ToolMissingError("go executable not found")