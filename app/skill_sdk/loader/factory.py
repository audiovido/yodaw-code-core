"""
Skill Loader Factory.

Factory for creating skill loaders based on source type.
"""

from typing import Any, Dict
import logging

from .dynamic import DynamicSkillLoader
from .resolver import SkillEntrypointResolver

logger = logging.getLogger(__name__)


class SkillLoaderFactory:
    """Factory for creating skill loaders."""

    def __init__(self):
        self._loader_cache: Dict[str, DynamicSkillLoader] = {}
        self._resolver = SkillEntrypointResolver()

    def create_loader(
        self,
        source_type: str,
        source_config: Dict[str, Any],
        cache_enabled: bool = True,
    ) -> DynamicSkillLoader:
        """Create a skill loader for the given source type."""
        cache_key = f"{source_type}:{hash(str(sorted(source_config.items())))}"

        if cache_enabled and cache_key in self._loader_cache:
            return self._loader_cache[cache_key]

        loader = DynamicSkillLoader(
            source_type=source_type,
            source_config=source_config,
            entrypoint_resolver=self._resolver,
        )

        if cache_enabled:
            self._loader_cache[cache_key] = loader

        logger.debug(f"Created skill loader for source type: {source_type}")
        return loader

    def clear_cache(self):
        """Clear the loader cache."""
        self._loader_cache.clear()
        logger.debug("Cleared skill loader cache")

    def get_cached_loaders(self) -> Dict[str, DynamicSkillLoader]:
        """Get cached loaders."""
        return self._loader_cache.copy()