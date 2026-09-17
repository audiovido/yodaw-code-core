"""
Dependency Checker.

Checks skill dependencies for compatibility and availability.
"""

from typing import Any, Dict, List, Optional
import logging

from ..models import SkillManifest, SkillDependency

logger = logging.getLogger(__name__)


class DependencyChecker:
    """Checks skill dependencies."""

    def __init__(self):
        self.logger = logger

    def check_dependencies(
        self,
        manifest: SkillManifest,
        available_skills: Optional[List[str]] = None
    ) -> List[str]:
        """Check skill dependencies for compatibility and availability.
        Returns list of dependency issues found.
        """
        issues = []

        # Check manifest dependencies
        manifest_deps_issues = self._check_manifest_dependencies(manifest)
        issues.extend(manifest_deps_issues)

        # Check compatibility dependencies
        compat_deps_issues = self._check_compatibility_dependencies(manifest, available_skills)
        issues.extend(compat_deps_issues)

        return issues

    def _check_manifest_dependencies(self, manifest: SkillManifest) -> List[str]:
        """Check dependencies listed in the manifest."""
        return self._check_dependencies(manifest.dependencies)

    def _check_compatibility_dependencies(
        self,
        manifest: SkillManifest,
        available_skills: Optional[List[str]] = None
    ) -> List[str]:
        """Check dependencies in the compatibility section."""
        return self._check_dependencies(manifest.compatibility.dependencies)

    def _check_dependencies(
        self,
        dependencies: List[SkillDependency],
    ) -> List[str]:
        issues = []
        for dep in dependencies:
            if not isinstance(dep.skill_id, str) or not dep.skill_id.strip():
                issues.append("Dependency missing skill_id")
                continue

            if not isinstance(dep.optional, bool):
                issues.append(f"Dependency {dep.skill_id} has invalid optional value")

            if dep.version_constraint and not isinstance(dep.version_constraint, str):
                issues.append(f"Dependency {dep.skill_id} has invalid version constraint")

        return issues

    def check_circular_dependencies(
        self,
        manifest: SkillManifest,
        all_manifests: Dict[str, SkillManifest]
    ) -> List[str]:
        """Check for circular dependencies among skills.
        Returns list of circular dependency issues found.
        """
        dependency_graph = {}
        for skill_id, skill_manifest in all_manifests.items():
            dependencies = list(skill_manifest.dependencies)
            dependencies.extend(skill_manifest.compatibility.dependencies)
            dependency_graph[skill_id] = [
                dep.skill_id for dep in dependencies if dep.skill_id
            ]

        visited = set()
        rec_stack = set()
        issues = []

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)

            for neighbor in dependency_graph.get(node, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True

            rec_stack.remove(node)
            return False

        for node in dependency_graph:
            if node not in visited and dfs(node):
                issues.append(f"Circular dependency detected involving skill: {node}")
                break

        return issues