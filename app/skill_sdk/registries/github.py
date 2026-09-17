"""
GitHub Skill Registry.

Registry for discovering skills from GitHub repositories.
"""


import base64
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
import logging

from .registry import SkillRegistry
from ..models import SkillManifest, SkillSource, SkillState

try:
    from github import Github, GithubException
    GITHUB_AVAILABLE = True
except ImportError:
    GITHUB_AVAILABLE = False

logger = logging.getLogger(__name__)


class GitHubSkillRegistry(SkillRegistry):
    """Registry for discovering skills from GitHub repositories."""

    def __init__(self, github_config: Dict[str, Any]):
        super().__init__("github")
        self.github_config = github_config
        self._github_client = None

    def _get_github_client(self):
        """Get or create GitHub client."""
        if not GITHUB_AVAILABLE:
            logger.warning("PyGithub not installed. GitHub registry disabled.")
            return None

        token = self.github_config.get("token")
        if token:
            return Github(token)
        elif self._github_client is None:
            # Try to create unauthenticated client (rate limited)
            self._github_client = Github()
        return self._github_client

    async def discover_skills(self) -> List[SkillManifest]:
        """Discover skills from GitHub repositories."""
        if not GITHUB_AVAILABLE:
            logger.error("GitHub discovery requires PyGithub package")
            return []

        skills = []
        token = self.github_config.get("token")
        repositories = self.github_config.get("repositories", [])

        if not repositories:
            logger.warning("No repositories specified for GitHub registry")
            return []

        client = self._get_github_client()
        if not client:
            return skills

        for repo_spec in repositories:
            try:
                repo_name = repo_spec.get("repository")
                branch = repo_spec.get("branch", "main")
                subdirectory = repo_spec.get("subdirectory", "")
                search_paths = repo_spec.get("search_paths", [""])

                if not repo_name:
                    logger.warning("Repository name is required")
                    continue

                # Get repository
                try:
                    repo = client.get_repo(repo_name)
                except GithubException as e:
                    logger.error(f"Failed to access repository {repo_name}: {e}")
                    continue

                # Search for skill manifests in each search path
                for search_path in search_paths:
                    path = f"{subdirectory}/{search_path}".strip("/")
                    try:
                        # Get contents of directory
                        contents = repo.get_contents(path, ref=branch)

                        # Look for skill manifest files
                        manifest_files = [
                            item for item in contents
                            if item.name in ["skill.yaml", "skill.yml", "manifest.yaml", "manifest.yml"]
                            and item.type == "file"
                        ]

                        for manifest_file in manifest_files:
                            try:
                                # Download and parse manifest
                                content = base64.b64decode(manifest_file.content).decode('utf-8')
                                data = yaml.safe_load(content)

                                if data and self._is_valid_manifest(data):
                                    manifest = SkillManifest.from_dict(data)
                                    manifest_dir = Path(manifest_file.path).parent
                                    if subdirectory:
                                        try:
                                            manifest_subdirectory = str(
                                                manifest_dir.relative_to(subdirectory)
                                            )
                                        except ValueError:
                                            manifest_subdirectory = str(manifest_dir)
                                    else:
                                        manifest_subdirectory = str(manifest_dir)
                                    manifest.source = SkillSource(
                                        type="github",
                                        url=f"https://github.com/{repo_name}",
                                        branch=branch,
                                        subdirectory=manifest_subdirectory,
                                        token=token,
                                        entrypoint=manifest.source.entrypoint,
                                    )
                                    manifest.state = SkillState.DISCOVERED
                                    skills.append(manifest)
                                    logger.debug(
                                        f"Discovered skill: {manifest.metadata.name} "
                                        f"from {repo_name}@{branch}"
                                    )
                            except Exception as e:
                                logger.error(
                                    f"Failed to load skill manifest {manifest_file.path}: {e}"
                                )

                    except GithubException as e:
                        if e.status == 404:
                            logger.debug(
                                f"Path {path} not found in {repo_name}@{branch}"
                            )
                        else:
                            logger.error(
                                f"Failed to access {path} in {repo_name}: {e}"
                            )

                logger.info(
                    f"Processed repository {repo_name}@{branch}"
                )

            except Exception as e:
                logger.error(
                    f"Failed to discover skills from GitHub repository {repo_spec}: {e}"
                )

        return skills

    async def get_skill(self, skill_id: str) -> Optional[SkillManifest]:
        """Get a specific skill by ID from GitHub registry."""
        skills = await self.discover_skills()
        for skill in skills:
            if skill.metadata.skill_id == skill_id:
                return skill
        return None

    async def search_skills(self, query: str) -> List[SkillManifest]:
        """Search for skills matching the query in GitHub registry."""
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