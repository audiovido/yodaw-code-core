"""
Skill Registries Package.
"""

from .local import LocalSkillRegistry
from .registry import SkillRegistry
from .git import GitSkillRegistry
from .github import GitHubSkillRegistry

__all__ = [
    "SkillRegistry",
    "LocalSkillRegistry",
    "GitSkillRegistry",
    "GitHubSkillRegistry",
]