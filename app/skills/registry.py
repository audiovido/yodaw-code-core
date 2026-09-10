"""
Skill registry for YODAW Coder Skills.

Manages registration, lookup, and lifecycle of skills.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import Skill, SkillId, Intent, RiskLevel, EvidenceSchema


@dataclass
class SkillRegistration:
    """Registration info for a skill."""
    skill: Skill
    factory: Optional[callable] = None  # Optional factory for creating skill instances
    metadata: dict[str, Any] = field(default_factory=dict)


class SkillRegistry:
    """Registry for managing skills."""

    def __init__(self):
        self._skills: dict[SkillId, SkillRegistration] = {}
        self._by_intent: dict[Intent, list[SkillId]] = {}
        self._by_language: dict[str, list[SkillId]] = {}
        self._by_tag: dict[str, list[SkillId]] = {}
        self._enabled_skills: set[SkillId] = set()

    def register(self, skill: Skill, factory: Optional[callable] = None, metadata: Optional[dict[str, Any]] = None) -> SkillId:
        """
        Register a skill.

        Args:
            skill: The skill to register
            factory: Optional factory function for creating skill instances
            metadata: Optional metadata

        Returns:
            The skill ID
        """
        registration = SkillRegistration(
            skill=skill,
            factory=factory,
            metadata=metadata or {},
        )

        self._skills[skill.id] = registration
        self._enabled_skills.add(skill.id)

        # Index by intent
        for intent in skill.supported_intents:
            if intent not in self._by_intent:
                self._by_intent[intent] = []
            self._by_intent[intent].append(skill.id)

        # Index by language
        for lang in skill.supported_languages:
            lang_lower = lang.lower()
            if lang_lower not in self._by_language:
                self._by_language[lang_lower] = []
            self._by_language[lang_lower].append(skill.id)

        # Index by tag
        for tag in skill.tags:
            tag_lower = tag.lower()
            if tag_lower not in self._by_tag:
                self._by_tag[tag_lower] = []
            self._by_tag[tag_lower].append(skill.id)

        return skill.id

    def unregister(self, skill_id: SkillId) -> bool:
        """
        Unregister a skill.

        Args:
            skill_id: The skill ID to unregister

        Returns:
            True if skill was found and removed
        """
        if skill_id not in self._skills:
            return False

        skill = self._skills[skill_id].skill

        # Remove from intent index
        for intent in skill.supported_intents:
            if intent in self._by_intent:
                self._by_intent[intent] = [s for s in self._by_intent[intent] if s != skill_id]

        # Remove from language index
        for lang in skill.supported_languages:
            lang_lower = lang.lower()
            if lang_lower in self._by_language:
                self._by_language[lang_lower] = [s for s in self._by_language[lang_lower] if s != skill_id]

        # Remove from tag index
        for tag in skill.tags:
            tag_lower = tag.lower()
            if tag_lower in self._by_tag:
                self._by_tag[tag_lower] = [s for s in self._by_tag[tag_lower] if s != skill_id]

        self._enabled_skills.discard(skill_id)
        del self._skills[skill_id]

        return True

    def get(self, skill_id: SkillId) -> Optional[Skill]:
        """Get a skill by ID."""
        registration = self._skills.get(skill_id)
        return registration.skill if registration else None

    def get_all(self) -> list[Skill]:
        """Get all registered skills."""
        return [reg.skill for reg in self._skills.values()]

    def get_enabled(self) -> list[Skill]:
        """Get all enabled skills."""
        return [self._skills[sid].skill for sid in self._enabled_skills if sid in self._skills]

    def get_by_intent(self, intent: Intent, enabled_only: bool = True) -> list[Skill]:
        """Get skills supporting a specific intent."""
        skill_ids = self._by_intent.get(intent, [])
        if enabled_only:
            skill_ids = [sid for sid in skill_ids if sid in self._enabled_skills]
        return [self._skills[sid].skill for sid in skill_ids if sid in self._skills]

    def get_by_language(self, language: str, enabled_only: bool = True) -> list[Skill]:
        """Get skills supporting a specific language."""
        lang_lower = language.lower()
        skill_ids = self._by_language.get(lang_lower, [])
        if enabled_only:
            skill_ids = [sid for sid in skill_ids if sid in self._enabled_skills]
        return [self._skills[sid].skill for sid in skill_ids if sid in self._skills]

    def get_by_tag(self, tag: str, enabled_only: bool = True) -> list[Skill]:
        """Get skills with a specific tag."""
        tag_lower = tag.lower()
        skill_ids = self._by_tag.get(tag_lower, [])
        if enabled_only:
            skill_ids = [sid for sid in skill_ids if sid in self._enabled_skills]
        return [self._skills[sid].skill for sid in skill_ids if sid in self._skills]

    def enable(self, skill_id: SkillId) -> bool:
        """Enable a skill."""
        if skill_id in self._skills:
            self._enabled_skills.add(skill_id)
            self._skills[skill_id].skill.enabled = True
            return True
        return False

    def disable(self, skill_id: SkillId) -> bool:
        """Disable a skill."""
        if skill_id in self._skills:
            self._enabled_skills.discard(skill_id)
            self._skills[skill_id].skill.enabled = False
            return True
        return False

    def is_enabled(self, skill_id: SkillId) -> bool:
        """Check if a skill is enabled."""
        return skill_id in self._enabled_skills

    def filter_skills(
        self,
        intent: Optional[Intent] = None,
        language: Optional[str] = None,
        tags: Optional[list[str]] = None,
        max_risk: Optional[RiskLevel] = None,
        enabled_only: bool = True,
    ) -> list[Skill]:
        """
        Filter skills by multiple criteria.

        Args:
            intent: Filter by intent
            language: Filter by language
            tags: Filter by tags (all must match)
            max_risk: Maximum risk level
            enabled_only: Only return enabled skills

        Returns:
            List of matching skills
        """
        candidates = self.get_enabled() if enabled_only else self.get_all()

        if intent:
            candidates = [s for s in candidates if intent in s.supported_intents]

        if language:
            lang_lower = language.lower()
            candidates = [s for s in candidates if s.matches_language(lang_lower)]

        if tags:
            tag_set = set(t.lower() for t in tags)
            candidates = [s for s in candidates if tag_set.issubset(set(t.lower() for t in s.tags))]

        if max_risk:
            risk_order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.CRITICAL: 3}
            max_level = risk_order[max_risk]
            candidates = [s for s in candidates if risk_order[s.risk] <= max_level]

        return candidates

    def get_skill_factory(self, skill_id: SkillId) -> Optional[callable]:
        """Get the factory function for a skill."""
        registration = self._skills.get(skill_id)
        return registration.factory if registration else None

    def create_skill_instance(self, skill_id: SkillId, **kwargs) -> Any:
        """Create an instance of a skill using its factory."""
        factory = self.get_skill_factory(skill_id)
        if factory:
            return factory(**kwargs)
        # Default: return the skill itself
        return self.get(skill_id)

    def list_skill_ids(self, enabled_only: bool = True) -> list[SkillId]:
        """List all skill IDs."""
        if enabled_only:
            return list(self._enabled_skills)
        return list(self._skills.keys())

    def count(self, enabled_only: bool = True) -> int:
        """Count registered skills."""
        return len(self._enabled_skills) if enabled_only else len(self._skills)

    def clear(self):
        """Clear all registrations."""
        self._skills.clear()
        self._by_intent.clear()
        self._by_language.clear()
        self._by_tag.clear()
        self._enabled_skills.clear()


def create_default_registry() -> SkillRegistry:
    """Create a registry with default skills pre-registered."""
    from .skills import (
        BugfixSkill,
        RefactorSkill,
        TestSkill,
        ReviewSkill,
        FeatureSkill,
        DependencySkill,
        DocumentationSkill,
        MigrationSkill,
        PerformanceSkill,
        SecuritySkill,
    )

    registry = SkillRegistry()

    # Register all default skills
    for skill_class in [
        BugfixSkill,
        RefactorSkill,
        TestSkill,
        ReviewSkill,
        FeatureSkill,
        DependencySkill,
        DocumentationSkill,
        MigrationSkill,
        PerformanceSkill,
        SecuritySkill,
    ]:
        skill = skill_class()
        registry.register(skill)

    return registry