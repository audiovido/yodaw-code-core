"""
Dynamic Skill Loader.

Loads skills dynamically from various sources and resolves their entrypoints.
"""

import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type
from urllib.parse import urlparse
from uuid import uuid4
import logging

from ..models import SkillManifest
from .resolver import SkillEntrypointResolver

logger = logging.getLogger(__name__)


class DynamicSkillLoader:
    """Loads skills dynamically from their configured source."""

    def __init__(
        self,
        source_type: str,
        source_config: Dict[str, Any],
        entrypoint_resolver: SkillEntrypointResolver,
    ):
        self.source_type = source_type
        self.source_config = source_config
        self._resolver = entrypoint_resolver
        self._loaded_skills: Dict[str, Type[Any]] = {}

    async def load_skill(self, manifest: SkillManifest) -> Type[Any]:
        """Load a skill class from its manifest."""
        skill_id = manifest.metadata.skill_id

        if skill_id in self._loaded_skills:
            return self._loaded_skills[skill_id]

        try:
            skill_class = await self._load_skill_class(manifest)
            self._loaded_skills[skill_id] = skill_class
            logger.info("Loaded skill: %s (%s)", manifest.metadata.name, skill_id)
            return skill_class
        except Exception as exc:
            logger.error("Failed to load skill %s: %s", manifest.metadata.name, exc)
            raise

    async def _load_skill_class(self, manifest: SkillManifest) -> Type[Any]:
        """Load the skill class from the manifest."""
        entrypoint = self._get_skill_entrypoint(manifest)
        if entrypoint:
            return self._load_class_from_entrypoint(
                entrypoint,
                self._get_source_path(manifest),
            )

        if self.source_type == "local":
            return await self._discover_local_skill_class(manifest)

        return await self._discover_skill_class(manifest)

    def _get_source_path(self, manifest: SkillManifest) -> Optional[Path]:
        """Return the configured local source path."""
        url = manifest.source.url or self.source_config.get("url")
        if not url:
            return None

        parsed = urlparse(url)
        if parsed.scheme == "file":
            return Path(parsed.path)
        if parsed.scheme:
            return None
        return Path(url)

    def _get_skill_entrypoint(self, manifest: SkillManifest) -> Optional[str]:
        """Get an explicit entrypoint from metadata, source, or loader config."""
        entrypoint = getattr(manifest.metadata, "entrypoint", None)
        if entrypoint:
            return entrypoint

        entrypoint = getattr(manifest.source, "entrypoint", None)
        if entrypoint:
            return entrypoint

        return self.source_config.get("entrypoint")

    def _load_class_from_entrypoint(
        self,
        entrypoint: str,
        source_path: Optional[Path] = None,
    ) -> Type[Any]:
        """Load a class from ``module.path:ClassName`` or a local module path."""
        try:
            module_path, class_name = entrypoint.split(":", 1)
        except ValueError as exc:
            raise ValueError(
                f"Invalid skill entrypoint {entrypoint!r}; expected 'module:Class'"
            ) from exc

        if not module_path.strip() or not class_name.strip():
            raise ValueError(f"Invalid skill entrypoint {entrypoint!r}")

        module = self._import_module(module_path.strip(), source_path)
        try:
            skill_class = getattr(module, class_name.strip())
        except AttributeError as exc:
            raise AttributeError(
                f"Skill class {class_name!r} was not found in {module_path!r}"
            ) from exc

        if not inspect.isclass(skill_class):
            raise TypeError(f"Entrypoint {entrypoint!r} does not reference a class")
        return skill_class

    def _import_module(
        self,
        module_path: str,
        source_path: Optional[Path] = None,
    ) -> Any:
        """Import a module, falling back to a file below a local source path."""
        try:
            return importlib.import_module(module_path)
        except ImportError as exc:
            if source_path is None or module_path.startswith("."):
                raise

            root = source_path if source_path.is_dir() else source_path.parent
            candidate = root / Path(*module_path.split(".")).with_suffix(".py")
            if not candidate.is_file():
                raise ImportError(
                    f"Could not import skill module {module_path!r} from {root}"
                ) from exc
            return self._load_module_file(candidate)

    def _load_module_file(self, path: Path) -> Any:
        """Load one source file under a unique module name."""
        module_name = f"skill_sdk_dynamic_{uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not create a module spec for {path}")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module

    async def _discover_local_skill_class(self, manifest: SkillManifest) -> Type[Any]:
        """Discover a skill class in a local source directory."""
        source_path = self._get_source_path(manifest)
        if source_path is None:
            raise ValueError(
                f"Could not discover skill class for {manifest.metadata.name}: "
                "local source path is empty"
            )

        for skill_file in self._iter_source_files(source_path):
            try:
                module = self._load_source_module(skill_file, source_path)
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if obj.__module__ != module.__name__:
                        continue
                    if self._resolver._is_likely_skill_class(obj):
                        logger.debug(
                            "Discovered local skill class %s in %s",
                            name,
                            skill_file,
                        )
                        return obj
            except Exception as exc:
                logger.debug("Could not load local skill module %s: %s", skill_file, exc)

        raise ValueError(
            f"Could not discover skill class for {manifest.metadata.name} "
            f"in source {manifest.source.url}"
        )

    def _load_source_module(self, path: Path, source_path: Path) -> Any:
        """Import a source module with its source root on ``sys.path``."""
        root = source_path if source_path.is_dir() else source_path.parent
        try:
            relative_path = path.relative_to(root)
        except ValueError:
            relative_path = Path(path.name)

        if relative_path.name == "__init__.py":
            module_path = ".".join(relative_path.parent.parts)
        else:
            module_path = ".".join(relative_path.with_suffix("").parts)

        if not module_path:
            return self._load_module_file(path)

        existing = sys.modules.get(module_path)
        existing_file = getattr(existing, "__file__", None)
        if existing is not None and existing_file:
            try:
                if Path(existing_file).resolve() != path.resolve():
                    sys.modules.pop(module_path, None)
            except OSError:
                sys.modules.pop(module_path, None)

        root_string = str(root)
        added_root = root_string not in sys.path
        if added_root:
            sys.path.insert(0, root_string)
        try:
            return importlib.import_module(module_path)
        finally:
            if added_root:
                try:
                    sys.path.remove(root_string)
                except ValueError:
                    pass

    @staticmethod
    def _iter_source_files(source_path: Path) -> List[Path]:
        """Return likely skill modules without loading them."""
        if source_path.is_file():
            return [source_path]

        patterns = ["*skill.py", "*_skill.py", "skill_*.py", "main.py", "__init__.py"]
        files: List[Path] = []
        seen = set()
        for pattern in patterns:
            for path in source_path.rglob(pattern):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    files.append(path)
        return files

    async def _discover_skill_class(self, manifest: SkillManifest) -> Type[Any]:
        """Discover a skill class using the legacy global skills convention."""
        try:
            module_name = f"skills.{manifest.metadata.skill_id}"
            module = importlib.import_module(module_name)
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if name.endswith("Skill") and hasattr(obj, "create_skill"):
                    return obj
        except ImportError:
            pass

        raise ValueError(
            f"Could not discover skill class for {manifest.metadata.name} "
            f"in source {manifest.source.url}"
        )

    async def unload_skill(self, skill_id: str) -> bool:
        """Unload a skill from the loader cache."""
        if skill_id in self._loaded_skills:
            del self._loaded_skills[skill_id]
            logger.info("Unloaded skill %s", skill_id)
            return True
        return False

    def is_skill_loaded(self, skill_id: str) -> bool:
        """Check if a skill is currently loaded."""
        return skill_id in self._loaded_skills

    def get_loaded_skills(self) -> Dict[str, Type[Any]]:
        """Get all loaded skills."""
        return self._loaded_skills.copy()

    def clear_cache(self):
        """Clear the loaded skills cache."""
        self._loaded_skills.clear()
        logger.debug("Cleared skill loader cache")
