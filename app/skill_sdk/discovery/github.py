"""
GitHub Skill Discovery.

Discovers skills from GitHub repositories using the GitHub API.
"""

import base64
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml
import logging

try:
    from github import Github, GithubException
    GITHUB_AVAILABLE = True
except ImportError:
    GITHUB_AVAILABLE = False

from ..models import SkillManifest, SkillSource, SkillState

logger = logging.getLogger(__name__)


class GitHubSkillDiscovery:
    """Discovers skills from GitHub repositories."""

    def __init__(self):
        self.logger = logger
        self._github_client = None

    def _get_github_client(self, token: Optional[str] = None):
        """Get or create GitHub client."""
        if not GITHUB_AVAILABLE:
            self.logger.warning("PyGithub not installed. GitHub discovery disabled.")
            return None

        if token:
            return Github(token)
        elif self._github_client is None:
            # Try to create unauthenticated client (rate limited)
            self._github_client = Github()
        return self._github_client

    async def discover_skills(self, github_config: Dict[str, Any]) -> List[SkillManifest]:
        """Discover skills from GitHub repositories."""
        if not GITHUB_AVAILABLE:
            self.logger.error("GitHub discovery requires PyGithub package")
            return []

        skills = []
        token = github_config.get("token")
        repositories = github_config.get("repositories", [])

        if not repositories:
            self.logger.warning("No repositories specified for GitHub discovery")
            return []

        client = self._get_github_client(token)
        if not client:
            return skills

        for repo_spec in repositories:
            try:
                repo_name = repo_spec.get("repository")
                branch = repo_spec.get("branch", "main")
                subdirectory = repo_spec.get("subdirectory", "")
                search_paths = repo_spec.get("search_paths", [""])

                if not repo_name:
                    self.logger.warning("Repository name is required")
                    continue

                # Get repository
                try:
                    repo = client.get_repo(repo_name)
                except GithubException as e:
                    self.logger.error(f"Failed to access repository {repo_name}: {e}")
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
                                            manifest_subdirectory = str(manifest_dir.relative_to(subdirectory))
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
                                    self.logger.debug(
                                        f"Discovered skill: {manifest.metadata.name} "
                                        f"from {repo_name}@{branch}"
                                    )
                            except Exception as e:
                                self.logger.error(
                                    f"Failed to load skill manifest {manifest_file.path}: {e}"
                                )

                    except GithubException as e:
                        if e.status == 404:
                            self.logger.debug(
                                f"Path {path} not found in {repo_name}@{branch}"
                            )
                        else:
                            self.logger.error(
                                f"Failed to access {path} in {repo_name}: {e}"
                            )

                self.logger.info(
                    f"Processed repository {repo_name}@{branch}"
                )

            except Exception as e:
                self.logger.error(
                    f"Failed to discover skills from GitHub repository {repo_spec}: {e}"
                )

        return skills

    def _is_valid_manifest(self, data: Dict[str, Any]) -> bool:
        """Check if the data appears to be a valid skill manifest."""
        required_fields = ["metadata", "source"]
        return all(field in data for field in required_fields)