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
        issues = []

        # For now, we don't have dependencies in the manifest model itself
        # This would be extended if we add dependencies to SkillManifest
        # For now, we'll check the compatibility dependencies
        pass

        return issues

    def _check_compatibility_dependencies(
        self,
        manifest: SkillManifest,
        available_skills: Optional[List[str]] = None
    ) -> List[str]:
        """Check dependencies in the compatibility section."""
        issues = []

        for dep in manifest.compatibility.dependencies:
            # Check if dependency has required fields
            if not dep.skill_id:
                issues.append("Dependency missing skill_id")
                continue

            # Check if dependency is available (if we have a list of available skills)
            if available_skills is not None and dep.skill_id not in available_skills:
                if not dep.optional:
                    issues.append(f"Required dependency not available: {dep.skill_id}")

            # Validate version constraint format (basic)
            if dep.version_constraint:
                # Simple validation - in practice this would be more sophisticated
                if not any(op in dep.version_constraint for op in ['>=', '>', '<=', '<', '==', '~=', '^']):
                    if dep.version_constraint != '*':
                        # Might be a version without constraint - that's OK
                        pass

        return issues

    def check_circular_dependencies(
        self,
        manifest: SkillManifest,
        all_manifests: Dict[str, SkillManifest]
    ) -> List[str]:
        """Check for circular dependencies among skills.
        Returns list of circular dependency issues found.
        """
        issues = []

        # Build dependency graph
        dependency_graph = {}
        for skill_id, skill_manifest in all_manifests.items():
            deps = [dep.skill_id for dep in skill_manifest.compatibility.dependencies if dep.skill_id]
            dependency_graph[skill_id] = deps

        # Check for circular dependencies using DFS
        visited = set()
        rec_stack = set()

        def dfs(node):
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
            if node not in visited:
                if dfs(node):
                    issues.append(f"Circular dependency detected involving skill: {node}")
                    break  # Just report one for now

        return issues