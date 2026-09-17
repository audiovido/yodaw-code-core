"""
Skill Discovery Orchestrator.

Orchestrates skill discovery from multiple sources and manages the discovery lifecycle.
"""

import asyncio
from typing import Any, Dict, List, Optional
import logging
from datetime import datetime

from .local import LocalSkillDiscovery
from .git import GitSkillDiscovery
from .github import GitHubSkillDiscovery
from ..registries.local import LocalSkillRegistry
from ..registries.git import GitSkillRegistry
from ..registries.github import GitHubSkillRegistry
from ..models import SkillManifest, SkillState
from ..validation.validator import SkillValidator

logger = logging.getLogger(__name__)


class SkillDiscoveryOrchestrator:
    """Orchestrates skill discovery from multiple sources."""

    def __init__(self):
        self.local_discovery = LocalSkillDiscovery()
        self.git_discovery = GitSkillDiscovery()
        self.github_discovery = GitHubSkillDiscovery()
        self.local_registry = LocalSkillRegistry([])
        self.git_registry = GitSkillRegistry([])
        self.github_registry = GitHubSkillRegistry([])
        self.validator = SkillValidator()
        self._discovered_skills: Dict[str, SkillManifest] = {}

    async def discover_all_skills(
        self,
        local_paths: Optional[List[str]] = None,
        git_repos: Optional[List[Dict[str, Any]]] = None,
        github_config: Optional[Dict[str, Any]] = None,
    ) -> List[SkillManifest]:
        """Discover skills from all configured sources."""
        all_skills = []

        # Discover local skills
        if local_paths:
            self.local_registry = LocalSkillRegistry(local_paths)
            local_skills = await self.local_registry.discover_skills()
            all_skills.extend(local_skills)
            logger.info(f"Discovered {len(local_skills)} local skills")

        # Discover git skills
        if git_repos:
            self.git_registry = GitSkillRegistry(git_repos)
            git_skills = await self.git_registry.discover_skills()
            all_skills.extend(git_skills)
            logger.info(f"Discovered {len(git_skills)} git skills")

        # Discover GitHub skills
        if github_config:
            self.github_registry = GitHubSkillRegistry(github_config)
            github_skills = await self.github_registry.discover_skills()
            all_skills.extend(github_skills)
            logger.info(f"Discovered {len(github_skills)} GitHub skills")

        # Validate and deduplicate skills
        validated_skills = await self._validate_and_deduplicate(all_skills)

        # Store discovered skills
        self._discovered_skills = {
            skill.metadata.skill_id: skill for skill in validated_skills
        }

        logger.info(f"Total validated skills discovered: {len(validated_skills)}")
        return validated_skills

    async def _validate_and_deduplicate(
        self, skills: List[SkillManifest]
    ) -> List[SkillManifest]:
        """Validate skills and remove duplicates."""
        validated = []
        seen_ids = set()

        for skill in skills:
            # Check for duplicates
            if skill.metadata.skill_id in seen_ids:
                logger.warning(
                    f"Duplicate skill ID found: {skill.metadata.skill_id}. "
                    f"Using first occurrence."
                )
                continue

            # Validate manifest
            validation_errors = self.validator.validate_manifest(skill)
            if validation_errors:
                skill.state = SkillState.ERROR
                skill.error_message = "Manifest validation failed"
                skill.error_details = {"validation_errors": validation_errors}
                logger.warning(
                    f"Skill {skill.metadata.name} ({skill.metadata.skill_id}) "
                    f"failed validation: {validation_errors}"
                )
            else:
                skill.state = SkillState.VALIDATED
                seen_ids.add(skill.metadata.skill_id)
                validated.append(skill)

        return validated

    async def get_skill(self, skill_id: str) -> Optional[SkillManifest]:
        """Get a specific skill by ID from discovered skills."""
        return self._discovered_skills.get(skill_id)

    async def search_skills(self, query: str) -> List[SkillManifest]:
        """Search for skills matching the query."""
        query_lower = query.lower()
        results = []

        for skill in self._discovered_skills.values():
            if (query_lower in skill.metadata.name.lower() or
                query_lower in skill.metadata.description.lower() or
                any(query_lower in tag.lower() for tag in skill.metadata.tags)):
                results.append(skill)

        return results

    async def get_skills_by_state(self, state: SkillState) -> List[SkillManifest]:
        """Get skills by their state."""
        return [
            skill for skill in self._discovered_skills.values()
            if skill.state == state
        ]

    async def refresh_skill(self, skill_id: str) -> bool:
        """Refresh a specific skill from its source."""
        skill = await self.get_skill(skill_id)
        if not skill:
            logger.warning(f"Skill {skill_id} not found for refresh")
            return False

        # TODO: Implement actual refresh logic based on skill source
        skill.last_checked = datetime.utcnow()
        logger.info(f"Refreshed skill {skill_id}")
        return True

    def get_discovery_stats(self) -> Dict[str, Any]:
        """Get statistics about discovered skills."""
        stats = {
            "total": len(self._discovered_skills),
            "by_state": {},
            "by_capability": {},
            "by_language": {},
        }

        for skill in self._discovered_skills.values():
            # Count by state
            state = skill.state.value
            stats["by_state"][state] = stats["by_state"].get(state, 0) + 1

            # Count by capabilities
            for cap in skill.metadata.capabilities:
                cap_val = cap.value
                stats["by_capability"][cap_val] = stats["by_capability"].get(cap_val, 0) + 1

            # Count by languages
            for lang in skill.metadata.languages:
                lang_val = lang.value
                stats["by_language"][lang_val] = stats["by_language"].get(lang_val, 0) + 1

        return stats