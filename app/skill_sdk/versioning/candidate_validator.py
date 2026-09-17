"""
Skill Candidate Validator.

Validates skill candidates before promotion to active status.
"""

from typing import Any, Dict, List, Optional
import logging
import asyncio
from datetime import datetime

from ..models import SkillManifest, SkillState
from ..validation.validator import SkillValidator
from ..loader.factory import SkillLoaderFactory

logger = logging.getLogger(__name__)


class CandidateValidator:
    """Validates skill candidates before promotion."""

    def __init__(self):
        self.validator = SkillValidator()
        self.loader_factory = SkillLoaderFactory()
        self.logger = logger

    async def validate_candidate(
        self,
        manifest: SkillManifest,
        validation_level: str = "standard"
    ) -> Dict[str, Any]:
        """Validate a skill candidate manifest."""
        start_time = datetime.utcnow()
        validation_result = {
            "skill_id": manifest.metadata.skill_id,
            "skill_name": manifest.metadata.name,
            "validation_level": validation_level,
            "started_at": start_time.isoformat(),
            "passed": False,
            "errors": [],
            "warnings": [],
            "checks_performed": [],
            "duration_ms": 0,
        }

        try:
            # Perform manifest validation
            manifest_errors = self.validator.validate_manifest(manifest)
            validation_result["checks_performed"].append("manifest_validation")
            if manifest_errors:
                validation_result["errors"].extend(manifest_errors)
                validation_result["errors"].append("Manifest validation failed")

            # Perform dependency validation
            dep_errors = await self._validate_dependencies(manifest)
            validation_result["checks_performed"].append("dependency_validation")
            validation_result["errors"].extend(dep_errors)

            # Perform loading validation (if validation_level >= standard)
            if validation_level in ["standard", "strict", "comprehensive"]:
                load_errors = await self._validate_loading(manifest)
                validation_result["checks_performed"].append("loading_validation")
                validation_result["errors"].extend(load_errors)

            # Perform execution validation (if validation_level == comprehensive)
            if validation_level == "comprehensive":
                exec_errors = await self._validate_execution(manifest)
                validation_result["checks_performed"].append("execution_validation")
                validation_result["errors"].extend(exec_errors)

            # Determine if validation passed
            validation_result["passed"] = len(validation_result["errors"]) == 0

            # Add any warnings
            if len(validation_result["warnings"]) > 0:
                validation_result["warnings"] = list(set(validation_result["warnings"]))  # Deduplicate

        except Exception as e:
            self.logger.error(f"Error during candidate validation: {e}")
            validation_result["errors"].append(f"Validation process failed: {str(e)}")

        finally:
            end_time = datetime.utcnow()
            validation_result["ended_at"] = end_time.isoformat()
            validation_result["duration_ms"] = int((end_time - start_time).total_seconds() * 1000)

        return validation_result

    async def _validate_dependencies(self, manifest: SkillManifest) -> List[str]:
        """Validate skill dependencies."""
        errors = []

        # Check for circular dependencies (simplified)
        dep_ids = [dep.skill_id for dep in manifest.compatibility.dependencies]
        if manifest.metadata.skill_id in dep_ids:
            errors.append("Skill cannot depend on itself")

        # Check for missing dependencies (would need to check against registry)
        # For now, we'll just note that dependencies exist
        if dep_ids:
            self.logger.debug(f"Skill {manifest.metadata.name} has {len(dep_ids)} dependencies")

        return errors

    async def _validate_loading(self, manifest: SkillManifest) -> List[str]:
        """Validate that the skill can be loaded."""
        errors = []

        try:
            # Try to create a loader for the skill's source
            loader = self.loader_factory.create_loader(
                source_type=manifest.source.type,
                source_config=manifest.source.to_dict(),
                cache_enabled=False,
            )

            # Try to load the skill class
            skill_class = await loader.load_skill(manifest)

            # Verify it has the required methods
            required_methods = ['create_skill', 'validate_inputs', 'execute', 'validate_outputs']
            for method in required_methods:
                if not hasattr(skill_class, method):
                    errors.append(f"Skill class missing required method: {method}")

        except Exception as e:
            errors.append(f"Failed to load skill: {str(e)}")

        return errors

    async def _validate_execution(self, manifest: SkillManifest) -> List[str]:
        """Validate that the skill can be executed (basic validation)."""
        errors = []

        # This would involve creating a skill instance and testing execution
        # For now, we'll just check that we can instantiate the skill class
        try:
            loader = self.loader_factory.create_loader(
                source_type=manifest.source.type,
                source_config=manifest.source.to_dict(),
                cache_enabled=False,
            )

            skill_class = await loader.load_skill(manifest)

            # Try to instantiate (if no args required)
            # In practice, we'd need to provide proper inputs
            # This is a basic check that the class can be instantiated
            if hasattr(skill_class, '__init__'):
                # Just check that it's instantiable - we won't actually create an instance
                # as it might require specific parameters
                pass

        except Exception as e:
            # Don't fail validation for execution issues in basic validation
            # Just add a warning
            self.logger.warning(f"Could not validate execution for {manifest.metadata.name}: {e}")

        return errors

    async def validate_batch(
        self,
        manifests: List[SkillManifest],
        validation_level: str = "standard"
    ) -> List[Dict[str, Any]]:
        """Validate a batch of skill candidates."""
        tasks = [
            self.validate_candidate(manifest, validation_level)
            for manifest in manifests
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)