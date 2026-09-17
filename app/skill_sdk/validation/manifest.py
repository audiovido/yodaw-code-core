"""
Manifest Validator.

Validates skill manifest structure and content.
"""

from typing import Any, Dict, List, Optional
import logging

from ..models import SkillManifest

logger = logging.getLogger(__name__)


class ManifestValidator:
    """Validates skill manifest structure."""

    def __init__(self):
        self.logger = logger

    def validate_manifest_structure(self, manifest: SkillManifest) -> List[str]:
        """Validate the basic structure of a skill manifest.
        Returns list of validation errors.
        """
        errors = []

        # Check required top-level sections
        if not manifest.metadata:
            errors.append("Manifest missing metadata section")
        if not manifest.source:
            errors.append("Manifest missing source section")

        # Validate metadata if present
        if manifest.metadata:
            errors.extend(self._validate_metadata(manifest.metadata))

        # Validate source if present
        if manifest.source:
            errors.extend(self._validate_source(manifest.source))

        return errors

    def _validate_metadata(self, metadata: Any) -> List[str]:
        """Validate skill metadata."""
        errors = []

        # Required fields
        if not metadata.name:
            errors.append("Metadata missing required field: name")
        if not metadata.description:
            errors.append("Metadata missing required field: description")
        if not metadata.skill_id:
            errors.append("Metadata missing required field: skill_id")

        # Validate version format
        if metadata.version:
            # Simple version validation - should be semantic versioning
            parts = metadata.version.split('.')
            if len(parts) < 2:
                errors.append("Metadata version should be in semantic versioning format (major.minor.patch)")

        return errors

    def _validate_source(self, source: Any) -> List[str]:
        """Validate skill source."""
        errors = []

        # Required fields
        if not source.type:
            errors.append("Source missing required field: type")
        if not source.url:
            errors.append("Source missing required field: url")

        # Validate source type
        valid_types = ["local", "git", "github"]
        if source.type and source.type not in valid_types:
            errors.append(f"Source type must be one of: {valid_types}")

        return errors

    def validate_manifest_content(self, manifest: SkillManifest) -> List[str]:
        """Validate the content of a skill manifest for consistency.
        Returns list of validation errors.
        """
        errors = []

        # Check consistency between metadata and other sections
        if manifest.metadata and manifest.compatibility:
            # Check that dependencies in compatibility are valid
            for dep in manifest.compatibility.dependencies:
                if not dep.skill_id:
                    errors.append("Dependency missing skill_id")

        return errors