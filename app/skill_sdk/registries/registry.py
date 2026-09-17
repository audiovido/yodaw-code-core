"""
Skill Registry Base Classes.

Provides the base registry interface and common functionality for skill registries.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional
import logging

from ..models import SkillManifest, SkillState


logger = logging.getLogger(__name__)


class SkillRegistry(ABC):
    """Abstract base class for skill registries."""

    def __init__(self, name: str):
        self.name = name
        self._skills: Dict[str, SkillManifest] = {}

    @abstractmethod
    async def discover_skills(self) -> List[SkillManifest]:
        """Discover skills from the registry source."""
        pass

    @abstractmethod
    async def get_skill(self, skill_id: str) -> Optional[SkillManifest]:
        """Get a specific skill by ID."""
        pass

    @abstractmethod
    async def search_skills(self, query: str) -> List[SkillManifest]:
        """Search for skills matching the query."""
        pass

    async def register_skill(self, manifest: SkillManifest) -> bool:
        """Register a skill in the registry."""
        try:
            self._skills[manifest.metadata.skill_id] = manifest
            logger.info(f"Registered skill {manifest.metadata.name} ({manifest.metadata.skill_id})")
            return True
        except Exception as e:
            logger.error(f"Failed to register skill {manifest.metadata.name}: {e}")
            return False

    async def unregister_skill(self, skill_id: str) -> bool:
        """Unregister a skill from the registry."""
        try:
            if skill_id in self._skills:
                del self._skills[skill_id]
                logger.info(f"Unregistered skill {skill_id}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to unregister skill {skill_id}: {e}")
            return False

    async def list_skills(self) -> List[SkillManifest]:
        """List all registered skills."""
        return list(self._skills.values())

    async def get_skills_by_state(self, state: SkillState) -> List[SkillManifest]:
        """Get skills by their state."""
        return [skill for skill in self._skills.values() if skill.state == state]

    async def update_skill_state(self, skill_id: str, state: SkillState) -> bool:
        """Update the state of a skill."""
        try:
            if skill_id in self._skills:
                self._skills[skill_id].state = state
                self._skills[skill_id].last_checked = datetime.utcnow()
                logger.info(f"Updated skill {skill_id} state to {state.value}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to update skill {skill_id} state: {e}")
            return False

    def _validate_manifest(self, manifest: SkillManifest) -> List[str]:
        """Validate a skill manifest and return list of errors."""
        errors = []

        # Validate required metadata fields
        if not manifest.metadata.name:
            errors.append("Skill name is required")
        if not manifest.metadata.description:
            errors.append("Skill description is required")
        if not manifest.metadata.skill_id:
            errors.append("Skill ID is required")

        # Validate source
        if not manifest.source.url:
            errors.append("Skill source URL is required")
        if not manifest.source.type:
            errors.append("Skill source type is required")

        return errors