"""
Skill system protocols and base abstractions.

Skills are structured runtime objects that encapsulate software-engineering
task patterns: bugfix, refactor, test, review, feature, documentation, etc.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


class SkillIntent(str, Enum):
    """Primary coding task intents."""
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    FEATURE = "feature"
    TEST = "test"
    REVIEW = "review"
    DOCUMENTATION = "documentation"
    DEPENDENCY = "dependency"
    MIGRATION = "migration"
    PERFORMANCE = "performance"
    SECURITY = "security"
    MIXED = "mixed"


class RiskLevel(str, Enum):
    """Skill execution risk classification."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class SkillClassification:
    """Result of task classification."""
    primary_skill: SkillIntent
    secondary_skills: list = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""


@dataclass
class SkillStep:
    """Single step in a skill execution plan."""
    step_id: str
    description: str
    skill_hint: Optional[SkillIntent] = None
    dependencies: list = field(default_factory=list)
    parallel_safe: bool = False
    validation_required: bool = True


@dataclass
class SkillPlan:
    """Decomposed multi-step execution plan."""
    steps: list
    bounded: bool = True
    max_steps: int = 10


@dataclass
class SkillEvidence:
    """Structured skill execution evidence."""
    event_type: str
    skill_id: str
    step_id: Optional[str] = None
    data: Optional[dict] = None


@runtime_checkable
class Skill(Protocol):
    """
    A skill encapsulates a software-engineering task pattern.
    
    Skills are NOT giant prompt strings; they are structured runtime
    objects providing guidance for planning, validation, and evidence.
    """
    
    @property
    def skill_id(self) -> str:
        """Unique skill identifier."""
        ...
    
    @property
    def description(self) -> str:
        """Human-readable skill description."""
        ...
    
    @property
    def supported_intents(self) -> list:
        """Task intents this skill handles."""
        ...
    
    @property
    def risk_level(self) -> RiskLevel:
        """Execution risk classification."""
        ...
    
    def planning_guidance(self) -> str:
        """Planning context for Coder Brain."""
        ...
    
    def validation_guidance(self) -> str:
        """Validation requirements for this skill."""
        ...
    
    def evidence_schema(self) -> dict:
        """Expected evidence structure."""
        ...
