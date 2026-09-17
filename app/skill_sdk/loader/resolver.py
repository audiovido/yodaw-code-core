"""
Skill Entrypoint Resolver.

Resolves skill entrypoints from various sources and formats.
"""

import importlib.util
import inspect
from pathlib import Path
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)


class SkillEntrypointResolver:
    """Resolves skill entrypoints from various sources."""

    def __init__(self):
        self.logger = logger

    def resolve_entrypoint(
        self,
        manifest: Any,  # SkillManifest
        source_path: Optional[Path] = None,
    ) -> Optional[str]:
        """Resolve the entrypoint for a skill."""
        # Try to get entrypoint from manifest
        entrypoint = self._get_manifest_entrypoint(manifest)
        if entrypoint:
            return entrypoint

        # Try to resolve from source files
        if source_path and source_path.exists():
            entrypoint = self._resolve_from_source(source_path)
            if entrypoint:
                return entrypoint

        # Try common conventions
        entrypoint = self._resolve_by_convention(manifest, source_path)
        if entrypoint:
            return entrypoint

        return None

    def _get_manifest_entrypoint(self, manifest: Any) -> Optional[str]:
        """Get entrypoint from manifest."""
        # Check metadata
        if hasattr(manifest.metadata, 'entrypoint') and manifest.metadata.entrypoint:
            return manifest.metadata.entrypoint

        # Check source
        if hasattr(manifest.source, 'entrypoint') and manifest.source.entrypoint:
            return manifest.source.entrypoint

        return None

    def _resolve_from_source(self, source_path: Path) -> Optional[str]:
        """Resolve entrypoint by scanning source files."""
        # Look for typical skill file patterns
        skill_files = []

        # Look for files that might contain skill implementations
        patterns = ["*skill.py", "*_skill.py", "skill_*.py"]
        for pattern in patterns:
            skill_files.extend(source_path.rglob(pattern))

        # Also check for main module files
        main_files = list(source_path.rglob("main.py")) + list(source_path.rglob("__init__.py"))
        skill_files.extend(main_files)

        for skill_file in skill_files:
            try:
                # Try to load the file and find skill classes
                spec = importlib.util.spec_from_file_location("skill_module", skill_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Look for classes that might be skills
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if self._is_likely_skill_class(obj):
                            # Return the entrypoint for this class
                            module_name = skill_file.relative_to(source_path).with_suffix("")
                            module_path = str(module_name).replace("/", ".")
                            return f"{module_path}:{name}"

            except Exception as e:
                self.logger.debug(f"Could not load {skill_file} for skill resolution: {e}")
                continue

        return None

    def _resolve_by_convention(
        self,
        manifest: Any,
        source_path: Optional[Path] = None,
    ) -> Optional[str]:
        """Resolve entrypoint by common conventions."""
        if not source_path:
            return None

        skill_name = manifest.metadata.name.lower().replace(" ", "_").replace("-", "_")
        skill_id = manifest.metadata.skill_id

        # Try common file names
        possible_files = [
            source_path / f"{skill_name}.py",
            source_path / f"{skill_id}.py",
            source_path / "skill.py",
            source_path / "__init__.py",
        ]

        for skill_file in possible_files:
            if skill_file.exists():
                try:
                    spec = importlib.util.spec_from_file_location("skill_module", skill_file)
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)

                        # Look for skill classes
                        for name, obj in inspect.getmembers(module, inspect.isclass):
                            if self._is_likely_skill_class(obj):
                                module_name = skill_file.relative_to(source_path).with_suffix("")
                                module_path = str(module_name).replace("/", ".")
                                return f"{module_path}:{name}"
                except Exception as e:
                    self.logger.debug(f"Could not load {skill_file} for convention resolution: {e}")
                    continue

        return None

    def _is_likely_skill_class(self, cls: Any) -> bool:
        """Check if a class is likely a skill implementation."""
        # Check if it has the typical skill methods
        required_methods = ['create_skill', 'validate_inputs', 'execute', 'validate_outputs']
        return all(hasattr(cls, method) for method in required_methods)