"""
Skill Safety Checker.

Provides safety checking for skill manifests and execution.
"""

from typing import Any, Dict, List, Optional
import logging

from ..models import SkillManifest

logger = logging.getLogger(__name__)


class SafetyChecker:
    """Checks skill manifests for safety concerns."""

    def __init__(self):
        self.logger = logger

    def check_manifest_safety(self, manifest: SkillManifest) -> List[str]:
        """Check a skill manifest for safety issues.
        Returns list of safety concerns found.
        """
        concerns = []

        # Check for risky permissions in source
        if manifest.source.type in ["git", "github"]:
            if manifest.source.token or manifest.source.username or manifest.source.password:
                concerns.append("Skill source contains credentials")

        # Check for risky capabilities
        risky_capabilities = ["security", "system"]
        for capability in manifest.metadata.capabilities:
            if capability.value in risky_capabilities:
                concerns.append(f"Skill has potentially risky capability: {capability.value}")

        # Check for risky tags
        risky_tags = ["root", "admin", "privileged", "system"]
        for tag in manifest.metadata.tags:
            if tag.lower() in risky_tags:
                concerns.append(f"Skill has potentially risky tag: {tag}")

        # Check for unknown license
        if manifest.metadata.license == manifest.metadata.license.__class__.UNKNOWN:
            concerns.append("Skill has unknown license")

        return concerns

    def is_manifest_safe(self, manifest: SkillManifest, strict: bool = False) -> bool:
        """Check if a manifest is considered safe."""
        concerns = self.check_manifest_safety(manifest)
        if strict:
            return len(concerns) == 0
        # In non-strict mode, only block critical concerns
        critical_concerns = [c for c in concerns if "credential" in c.lower() or "system" in c.lower()]
        return len(critical_concerns) == 0