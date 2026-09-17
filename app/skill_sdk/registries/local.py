"""
from typing import Optional
Local Skill Registry.

Registry for discovering skills from the local filesystem.
"""
from typing import Optional

from pathlib import Path
from typing import Any, Dict, List
import yaml
import logging

from .registry import SkillRegistry
from ..models import SkillManifest, SkillSource, SkillState

logger = logging.getLogger(__name__)


class LocalSkillRegistry(SkillRegistry):
    """Registry for discovering skills from local filesystem."""

    def __init__(self, search_paths: List[str]):
        super().__init__("local")
        self.search_paths = [Path(p) for p in search_paths]

    async def discover_skills(self) -> List[SkillManifest]:
        """Discover skills from local filesystem."""
        skills = []

        for search_path in self.search_paths:
            if not search_path.exists():
                logger.warning(f"Search path does not exist: {search_path}")
                continue

            # Look for skill manifest files
            manifest_files = list(search_path.rglob("skill.yaml")) + \
                           list(search_path.rglob("skill.yml")) + \
                           list(search_path.rglob("manifest.yaml")) + \
                           list(search_path.rglob("manifest.yml"))

            for manifest_file in manifest_files:
                try:
                    with open(manifest_file, 'r') as f:
                        data = yaml.safe_load(f)

                    if data and self._is_valid_manifest(data):
                        manifest = SkillManifest.from_dict(data)
                        manifest.source = SkillSource(
                            type="local",
                            url=str(manifest_file.parent),
                            subdirectory=str(manifest_file.parent.relative_to(search_path)),
                            entrypoint=manifest.source.entrypoint,
                        )
                        manifest.state = SkillState.DISCOVERED
                        skills.append(manifest)
                        logger.debug(f"Discovered skill: {manifest.metadata.name}")
                except Exception as e:
                    logger.error(f"Failed to load skill manifest {manifest_file}: {e}")

        return skills

    async def get_skill(self, skill_id: str) -> Optional[SkillManifest]:
        """Get a specific skill by ID from local registry."""
        skills = await self.discover_skills()
        for skill in skills:
            if skill.metadata.skill_id == skill_id:
                return skill
        return None

    async def search_skills(self, query: str) -> List[SkillManifest]:
        """Search for skills matching the query in local registry."""
        skills = await self.discover_skills()
        query_lower = query.lower()

        results = []
        for skill in skills:
            if (query_lower in skill.metadata.name.lower() or
                query_lower in skill.metadata.description.lower() or
                any(query_lower in tag.lower() for tag in skill.metadata.tags)):
                results.append(skill)

        return results

    def _is_valid_manifest(self, data: Dict[str, Any]) -> bool:
        """Check if the data appears to be a valid skill manifest."""
        required_fields = ["metadata", "source"]
        return all(field in data for field in required_fields)