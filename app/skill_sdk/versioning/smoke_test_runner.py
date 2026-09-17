"""
Skill Smoke Test Runner.

Runs smoke tests for skills to verify basic functionality.
"""

import asyncio
import time
from typing import Any, Dict, List, Optional
import logging

from ..models import SkillManifest, SkillState
from ..loader.factory import SkillLoaderFactory

logger = logging.getLogger(__name__)


class SmokeTestRunner:
    """Runs smoke tests for skills."""

    def __init__(self):
        self.loader_factory = SkillLoaderFactory()
        self.logger = logger

    async def run_smoke_test(
        self,
        manifest: SkillManifest,
        timeout_seconds: int = 30
    ) -> Dict[str, Any]:
        """Run a smoke test for a skill."""
        start_time = time.time()
        test_result = {
            "skill_id": manifest.metadata.skill_id,
            "skill_name": manifest.metadata.name,
            "started_at": time.time(),
            "passed": False,
            "error": None,
            "duration_ms": 0,
            "steps_completed": [],
        }

        try:
            # Step 1: Load the skill
            loader = self.loader_factory.create_loader(
                source_type=manifest.source.type,
                source_config=manifest.source.to_dict(include_credentials=False),
                cache_enabled=False,
            )

            skill_class = await loader.load_skill(manifest)
            test_result["steps_completed"].append("skill_loading")

            # Step 2: Instantiate the skill
            skill_instance = skill_class()
            test_result["steps_completed"].append("skill_instantiation")

            # Step 3: Create the skill definition
            skill_def = skill_instance.create_skill()
            test_result["steps_completed"].append("skill_creation")

            # Step 4: Validate the skill definition
            # This is a basic validation - in practice we'd check the skill definition
            if skill_def and hasattr(skill_def, 'metadata'):
                test_result["steps_completed"].append("skill_definition_validation")

            # If we got here, the smoke test passes
            test_result["passed"] = True

        except Exception as e:
            self.logger.error(f"Smoke test failed for {manifest.metadata.name}: {e}")
            test_result["error"] = str(e)

        finally:
            end_time = time.time()
            test_result["ended_at"] = end_time
            test_result["duration_ms"] = int((end_time - start_time) * 1000)

        return test_result

    async def run_batch_smoke_tests(
        self,
        manifests: List[SkillManifest],
        timeout_seconds: int = 30,
        max_concurrent: int = 5
    ) -> List[Dict[str, Any]]:
        """Run smoke tests for a batch of skills."""
        semaphore = asyncio.Semaphore(max_concurrent)

        async def run_with_semaphore(manifest):
            async with semaphore:
                return await self.run_smoke_test(manifest, timeout_seconds)

        tasks = [run_with_semaphore(manifest) for manifest in manifests]
        return await asyncio.gather(*tasks, return_exceptions=True)