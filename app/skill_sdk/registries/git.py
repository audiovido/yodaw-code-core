"""
Git Skill Registry.

Registry for discovering skills from git repositories.
"""
from typing import Optional

import subprocess
from pathlib import Path
from typing import Any, Dict, List
import yaml
import logging
import tempfile

from .registry import SkillRegistry
from ..models import SkillManifest, SkillSource, SkillState

logger = logging.getLogger(__name__)


class GitSkillRegistry(SkillRegistry):
    """Registry for discovering skills from git repositories."""

    def __init__(self, git_repos: List[Dict[str, Any]]):
        super().__init__("git")
        self.git_repos = git_repos

    async def discover_skills(self) -> List[SkillManifest]:
        """Discover skills from git repositories."""
        skills = []

        for repo_config in self.git_repos:
            try:
                repo_url = repo_config.get("url")
                branch = repo_config.get("branch", "main")
                subdirectory = repo_config.get("subdirectory", "")
                credentials = repo_config.get("credentials", {})

                if not repo_url:
                    logger.warning("Git repository URL is required")
                    continue

                # Clone repository to temporary directory
                with tempfile.TemporaryDirectory() as temp_dir:
                    repo_path = Path(temp_dir) / "repo"

                    # Build clone command
                    clone_cmd = ["git", "clone", "--branch", branch, "--depth", "1"]
                    if credentials.get("token"):
                        # For token-based auth, we modify the URL
                        if repo_url.startswith("https://"):
                            token_url = repo_url.replace(
                                "https://", f"https://{credentials['token']}@"
                            )
                            clone_cmd.extend([token_url, str(repo_path)])
                        else:
                            clone_cmd.extend([repo_url, str(repo_path)])
                    else:
                        clone_cmd.extend([repo_url, str(repo_path)])

                    # Execute clone
                    result = subprocess.run(
                        clone_cmd,
                        capture_output=True,
                        text=True,
                        timeout=300  # 5 minute timeout
                    )

                    if result.returncode != 0:
                        logger.error(
                            f"Failed to clone repository {repo_url}: {result.stderr}"
                        )
                        continue

                    # Navigate to subdirectory if specified
                    search_path = repo_path
                    if subdirectory:
                        search_path = repo_path / subdirectory
                        if not search_path.exists():
                            logger.warning(
                                f"Subdirectory {subdirectory} not found in {repo_url}"
                            )
                            continue

                    # Discover skills in the cloned repository
                    repo_skills = await self._discover_skills_in_path(
                        search_path,
                        repo_url,
                        branch,
                    )
                    skills.extend(repo_skills)

                    logger.info(
                        f"Discovered {len(repo_skills)} skills from {repo_url}"
                    )

            except subprocess.TimeoutExpired:
                logger.error(f"Timeout cloning repository {repo_url}")
            except Exception as e:
                logger.error(
                    f"Failed to discover skills from git repository {repo_url}: {e}"
                )

        return skills

    async def get_skill(self, skill_id: str) -> Optional[SkillManifest]:
        """Get a specific skill by ID from git registry."""
        skills = await self.discover_skills()
        for skill in skills:
            if skill.metadata.skill_id == skill_id:
                return skill
        return None

    async def search_skills(self, query: str) -> List[SkillManifest]:
        """Search for skills matching the query in git registry."""
        skills = await self.discover_skills()
        query_lower = query.lower()

        results = []
        for skill in skills:
            if (query_lower in skill.metadata.name.lower() or
                query_lower in skill.metadata.description.lower() or
                any(query_lower in tag.lower() for tag in skill.metadata.tags)):
                results.append(skill)

        return results

    async def _discover_skills_in_path(self, search_path: Path) -> List[SkillManifest]:
        """Discover skills in a given filesystem path."""
        skills = []

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
                        type="git",
                        url=str(search_path),  # This would be the actual repo URL
                        branch="main",  # Would be from config
                        subdirectory=str(
                            manifest_file.parent.relative_to(search_path)
                        ),
                    )
                    manifest.state = SkillState.DISCOVERED
                    skills.append(manifest)
                    logger.debug(f"Discovered skill: {manifest.metadata.name}")
            except Exception as e:
                logger.error(f"Failed to load skill manifest {manifest_file}: {e}")

        return skills

    def _is_valid_manifest(self, data: Dict[str, Any]) -> bool:
        """Check if the data appears to be a valid skill manifest."""
        required_fields = ["metadata", "source"]
        return all(field in data for field in required_fields)