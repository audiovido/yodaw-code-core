"""
Local Skill Discovery.

Discovers skills from the local filesystem.
"""

from pathlib import Path
from typing import Any, Dict, List
import yaml
import logging

from ..models import SkillManifest, SkillSource, SkillState

logger = logging.getLogger(__name__)


class LocalSkillDiscovery:
    """Discovers skills from local filesystem."""

    def __init__(self):
        self.logger = logger

    async def discover_skills(self, search_paths: List[str]) -> List[SkillManifest]:
        """Discover skills from local filesystem paths."""
        skills = []

        for search_path_str in search_paths:
            search_path = Path(search_path_str)
            if not search_path.exists():
                self.logger.warning(f"Search path does not exist: {search_path}")
                continue

            # Look for skill manifest files
            manifest_files = []
            manifest_files.extend(search_path.rglob("skill.yaml"))
            manifest_files.extend(search_path.rglob("skill.yml"))
            manifest_files.extend(search_path.rglob("manifest.yaml"))
            manifest_files.extend(search_path.rglob("manifest.yml"))

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
                        self.logger.debug(f"Discovered skill: {manifest.metadata.name}")
                except Exception as e:
                    self.logger.error(f"Failed to load skill manifest {manifest_file}: {e}")

        return skills

    def _is_valid_manifest(self, data: Dict[str, Any]) -> bool:
        """Check if the data appears to be a valid skill manifest."""
        required_fields = ["metadata", "source"]
        return all(field in data for field in required_fields)