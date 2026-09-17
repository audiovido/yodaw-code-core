"""
Skill Version Manager.

Manages skill versions, updates, and compatibility checking.
"""

from typing import Any, Dict, List, Optional, Tuple
import logging
from datetime import datetime

from ..models import SkillManifest, SkillState, SkillCompatibility

logger = logging.getLogger(__name__)


class VersionManager:
    """Manages skill versions and updates."""

    def __init__(self):
        self.logger = logger

    def compare_versions(self, version1: str, version2: str) -> int:
        """Compare two semantic versions.
        Returns: -1 if v1 < v2, 0 if v1 == v2, 1 if v1 > v2
        """
        def parse_version(v: str) -> List[int]:
            # Remove any non-numeric suffixes for comparison
            v_clean = v.split('-')[0].split('+')[0]
            parts = []
            for part in v_clean.split('.'):
                try:
                    parts.append(int(part))
                except ValueError:
                    parts.append(0)  # Non-numeric parts treated as 0
            return parts

        v1_parts = parse_version(version1)
        v2_parts = parse_version(version2)

        # Pad with zeros to make equal length
        max_len = max(len(v1_parts), len(v2_parts))
        v1_parts.extend([0] * (max_len - len(v1_parts)))
        v2_parts.extend([0] * (max_len - len(v2_parts)))

        # Compare each part
        for i in range(max_len):
            if v1_parts[i] < v2_parts[i]:
                return -1
            elif v1_parts[i] > v2_parts[i]:
                return 1
        return 0

    def is_version_compatible(
        self,
        current_version: str,
        required_version: str,
        constraint: str = ""
    ) -> bool:
        """Check if current version satisfies the required version constraint."""
        if not constraint:
            # If no constraint, assume exact version required
            return self.compare_versions(current_version, required_version) == 0

        # Simple constraint parsing - in practice, this would be more sophisticated
        if constraint.startswith(">="):
            required = constraint[2:].strip()
            return self.compare_versions(current_version, required) >= 0
        elif constraint.startswith(">"):
            required = constraint[1:].strip()
            return self.compare_versions(current_version, required) > 0
        elif constraint.startswith("<="):
            required = constraint[2:].strip()
            return self.compare_versions(current_version, required) <= 0
        elif constraint.startswith("<"):
            required = constraint[1:].strip()
            return self.compare_versions(current_version, required) < 0
        elif constraint.startswith("==") or constraint.startswith("="):
            required = constraint[1:].strip() if constraint.startswith("=") else constraint[2:].strip()
            return self.compare_versions(current_version, required) == 0
        elif constraint.startswith("~>"):
            # Pessimistic version constraint (same major and minor, but patch >= required)
            required = constraint[2:].strip()
            # For simplicity, we'll treat this as >= required for now
            return self.compare_versions(current_version, required) >= 0
        elif constraint.startswith("^"):
            # Caret constraint (same major version, but >= required)
            required = constraint[1:].strip()
            # For simplicity, we'll treat this as >= required for now
            return self.compare_versions(current_version, required) >= 0
        else:
            # Treat as exact match
            return self.compare_versions(current_version, constraint) == 0

    def get_latest_version(self, versions: List[str]) -> Optional[str]:
        """Get the latest version from a list of versions."""
        if not versions:
            return None

        latest = versions[0]
        for version in versions[1:]:
            if self.compare_versions(version, latest) > 0:
                latest = version
        return latest

    def sort_versions(self, versions: List[str], descending: bool = True) -> List[str]:
        """Sort versions by semantic versioning."""
        return sorted(versions, key=lambda v: [int(x) for x in v.split('.')], reverse=descending)

    def check_update_available(
        self,
        current_manifest: SkillManifest,
        latest_manifest: SkillManifest
    ) -> Tuple[bool, str]:
        """Check if an update is available for a skill."""
        comparison = self.compare_versions(
            current_manifest.metadata.version,
            latest_manifest.metadata.version
        )

        if comparison < 0:
            return True, f"Update available: {current_manifest.metadata.version} -> {latest_manifest.metadata.version}"
        elif comparison > 0:
            return False, f"Current version is newer: {current_manifest.metadata.version} > {latest_manifest.metadata.version}"
        else:
            return False, "Versions are identical"

    def validate_version_constraints(
        self,
        manifest: SkillManifest
    ) -> List[str]:
        """Validate version constraints in skill manifest."""
        errors = []

        # Check that version is present and valid
        if not manifest.metadata.version:
            errors.append("Skill version is required")

        # Check compatibility dependencies
        for dep in manifest.compatibility.dependencies:
            if not dep.skill_id:
                errors.append("Dependency skill ID is required")
            # Version constraint validation would go here

        return errors