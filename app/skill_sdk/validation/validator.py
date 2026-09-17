"""
Skill Validator.

Provides validation functionality for skill manifests and skill execution.
"""

from typing import Any, Dict, List
import logging
from ..models import (
    SkillCapability,
    SkillLanguage,
    SkillLicense,
    SkillManifest,
    SkillRiskLevel,
    SkillState,
)

logger = logging.getLogger(__name__)


class SkillValidator:
    """Validates skill manifests and skill execution."""

    _SKILL_STATES = {state.value for state in SkillState}
    _CAPABILITIES = {capability.value for capability in SkillCapability}
    _LANGUAGES = {language.value for language in SkillLanguage}
    _LICENSES = {license.value for license in SkillLicense}
    _RISK_LEVELS = {level.value for level in SkillRiskLevel}
    _SOURCE_TYPES = {"local", "git", "github"}

    def validate_manifest(self, manifest: SkillManifest) -> List[str]:
        """Validate a skill manifest and return list of errors."""
        errors = []

        try:
            metadata = manifest.metadata
            source = manifest.source

            for field, value in (
                ("metadata.name", metadata.name),
                ("metadata.description", metadata.description),
                ("metadata.skill_id", metadata.skill_id),
                ("metadata.version", metadata.version),
                ("source.type", source.type),
                ("source.url", source.url),
            ):
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{field} is required")

            if source.type and source.type not in self._SOURCE_TYPES:
                errors.append(f"source.type must be one of: {sorted(self._SOURCE_TYPES)}")
            if metadata.license.value not in self._LICENSES:
                errors.append("metadata.license is invalid")
            if metadata.risk_level.value not in self._RISK_LEVELS:
                errors.append("metadata.risk_level is invalid")
            if manifest.state.value not in self._SKILL_STATES:
                errors.append("state is invalid")

            invalid_capabilities = [
                capability.value for capability in metadata.capabilities
                if capability.value not in self._CAPABILITIES
            ]
            if invalid_capabilities:
                errors.append(f"metadata.capabilities contains invalid values: {invalid_capabilities}")

            invalid_languages = [
                language.value for language in metadata.languages
                if language.value not in self._LANGUAGES
            ]
            if invalid_languages:
                errors.append(f"metadata.languages contains invalid values: {invalid_languages}")

            dependencies = list(manifest.dependencies)
            dependencies.extend(manifest.compatibility.dependencies)
            for index, dependency in enumerate(dependencies):
                if not isinstance(dependency.skill_id, str) or not dependency.skill_id.strip():
                    errors.append(f"dependency[{index}].skill_id is required")

        except Exception as e:
            logger.error(f"Error validating manifest: {e}")
            errors.append(f"Manifest validation failed: {str(e)}")

        return errors

    def validate_skill_inputs(self, skill_id: str, inputs: Dict[str, Any]) -> List[str]:
        """Validate inputs for a skill execution."""
        return []

    def validate_skill_outputs(self, skill_id: str, outputs: Dict[str, Any]) -> List[str]:
        """Validate outputs from a skill execution."""
        return []

    def is_manifest_valid(self, manifest: SkillManifest) -> bool:
        """Check if a manifest is valid."""
        return not self.validate_manifest(manifest)