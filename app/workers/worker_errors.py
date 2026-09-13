"""Shared worker error types: cancellation, tooling, and subprocess failures."""


class MissionCancelled(Exception):
    """Raised at cancellation checkpoints inside a worker mission.

    Carries the checkpoint name so callers can report where
    cancellation was observed.
    """

    def __init__(self, at: str = ""):
        self.at = at
        super().__init__(at or "checkpoint")


class ToolMissingError(RuntimeError):
    """A validation or execution tool is not available in the environment."""


class SubprocessTimeout(TimeoutError):
    """A bounded subprocess exceeded its deadline and was terminated."""

class MissionTimeout(TimeoutError):
    """The mission's own deadline expired; no commit may follow."""