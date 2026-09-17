"""
Skill Update Architecture.

Defines different strategies for updating skills.
"""

from enum import Enum
from typing import Any, Dict, List, Optional
import logging

from ..models import SkillManifest

logger = logging.getLogger(__name__)


class UpdateStrategy(str, Enum):
    """Strategies for updating skills."""
    ROLLING = "rolling"
    BLUE_GREEN = "blue_green"
    CANARY = "canary"
    RECREATE = "recreate"


class UpdateArchitecture:
    """Defines the architecture for skill updates."""

    def __init__(
        self,
        strategy: UpdateStrategy = UpdateStrategy.ROLLING,
        batch_size: int = 1,
        max_surge: int = 0,
        max_unavailable: int = 0,
        min_ready_seconds: int = 0,
        timeout_seconds: int = 300,
    ):
        self.strategy = strategy
        self.batch_size = batch_size
        self.max_surge = max_surge
        self.max_unavailable = max_unavailable
        self.min_ready_seconds = min_ready_seconds
        self.timeout_seconds = timeout_seconds
        self.logger = logger

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "strategy": self.strategy.value,
            "batch_size": self.batch_size,
            "max_surge": self.max_surge,
            "max_unavailable": self.max_unavailable,
            "min_ready_seconds": self.min_ready_seconds,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UpdateArchitecture":
        """Create from dictionary representation."""
        return cls(
            strategy=UpdateStrategy(data.get("strategy", "rolling")),
            batch_size=data.get("batch_size", 1),
            max_surge=data.get("max_surge", 0),
            max_unavailable=data.get("max_unavailable", 0),
            min_ready_seconds=data.get("min_ready_seconds", 0),
            timeout_seconds=data.get("timeout_seconds", 300),
        )

    def is_valid(self) -> bool:
        """Validate the update architecture configuration."""
        if self.batch_size < 1:
            return False
        if self.max_surge < 0:
            return False
        if self.max_unavailable < 0:
            return False
        if self.min_ready_seconds < 0:
            return False
        if self.timeout_seconds < 1:
            return False
        return True

    def get_rollback_strategy(self) -> UpdateStrategy:
        """Get the appropriate rollback strategy for this update strategy."""
        # Most strategies can rollback using the same strategy
        return self.strategy