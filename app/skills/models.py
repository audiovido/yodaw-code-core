"""
Core models for the YODAW Coder Skill system.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from uuid import uuid4


class Intent(str, Enum):
    """Supported intent types for skills."""
    BUGFIX = "bugfix"
    REFACTOR = "refactor"
    TEST = "test"
    REVIEW = "review"
    FEATURE = "feature"
    DEPENDENCY = "dependency"
    DOCUMENTATION = "documentation"
    MIGRATION = "migration"
    PERFORMANCE = "performance"
    SECURITY = "security"


class RiskLevel(str, Enum):
    """Risk levels for skill execution."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SkillId = str


@dataclass
class SkillInput:
    """Input specification for a skill."""
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None


@dataclass
class SkillPrerequisite:
    """Prerequisite for a skill."""
    name: str
    description: str
    check: str  # Function name to call for checking


@dataclass
class PlanningGuidance:
    """Guidance for planning with this skill."""
    typical_steps: list[str]
    common_pitfalls: list[str]
    estimated_duration_minutes: int
    requires_human_review: bool = False


@dataclass
class ValidationGuidance:
    """Guidance for validating skill output."""
    success_criteria: list[str]
    validation_commands: list[str]
    rollback_guidance: str = ""


@dataclass
class EvidenceSchema:
    """Schema for skill execution evidence."""
    required_fields: list[str]
    optional_fields: list[str] = field(default_factory=list)
    artifact_types: list[str] = field(default_factory=list)


@dataclass
class SkillEvidence:
    """Evidence produced by skill execution."""
    skill_id: SkillId
    execution_id: str
    timestamp: str
    success: bool
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    artifacts: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    """A modular software engineering skill."""
    id: SkillId
    name: str
    description: str
    supported_intents: list[Intent]
    inputs: list[SkillInput]
    prerequisites: list[SkillPrerequisite]
    planning_guidance: PlanningGuidance
    validation_guidance: ValidationGuidance
    risk: RiskLevel
    evidence_schema: EvidenceSchema
    supported_languages: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    enabled: bool = True

    def __post_init__(self):
        if not self.id:
            self.id = f"skill-{uuid4().hex[:8]}"

    def matches_intent(self, intent: Intent) -> bool:
        """Check if this skill supports the given intent."""
        return intent in self.supported_intents

    def matches_language(self, language: str) -> bool:
        """Check if this skill supports the given language."""
        return not self.supported_languages or language.lower() in [l.lower() for l in self.supported_languages]


@dataclass
class ClassificationResult:
    """Result of task classification."""
    primary_skill: SkillId
    secondary_skills: list[SkillId]
    confidence: float
    reason: str
    detected_intent: Intent
    detected_language: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SelectionResult:
    """Result of skill selection."""
    selected_skill: SkillId
    alternatives: list[SkillId]
    confidence: float
    reason: str


@dataclass
class DecompositionStep:
    """A single step in task decomposition."""
    step_id: str
    skill_id: SkillId
    description: str
    inputs: dict[str, Any]
    depends_on: list[str] = field(default_factory=list)
    estimated_duration_minutes: int = 30
    requires_approval: bool = False
    rollback_step_id: Optional[str] = None


@dataclass
class DecompositionResult:
    """Result of task decomposition."""
    steps: list[DecompositionStep]
    total_estimated_minutes: int
    requires_human_review: bool
    risk_level: RiskLevel
    metadata: dict[str, Any] = field(default_factory=dict)