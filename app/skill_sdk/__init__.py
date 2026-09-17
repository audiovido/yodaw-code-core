"""
Codgar Universal Skill SDK.

Provides reusable skill discovery, registration, and management capabilities
for the Codgar worker system.
"""

from .models import (
    SkillManifest,
    SkillMetadata,
    SkillSource,
    SkillState,
    SkillDependency,
    SkillCapability,
    SkillLanguage,
    SkillRiskLevel,
    SkillLicense,
    SkillChecksum,
    SkillProvenance,
    SkillCompatibility,
)
from .registries import SkillRegistry, LocalSkillRegistry, GitSkillRegistry, GitHubSkillRegistry
from .discovery import (
    LocalSkillDiscovery,
    GitSkillDiscovery,
    GitHubSkillDiscovery,
    SkillDiscoveryOrchestrator,
)
from .validation import (
    SkillValidator,
)
from .loader import (
    DynamicSkillLoader,
    SkillLoaderFactory,
    SkillEntrypointResolver,
)
from .versioning import (
    VersionManager,
    UpdateArchitecture,
    CandidateValidator,
    SmokeTestRunner,
)

__all__ = [
    # Models
    "SkillManifest",
    "SkillMetadata",
    "SkillSource",
    "SkillState",
    "SkillDependency",
    "SkillCapability",
    "SkillLanguage",
    "SkillRiskLevel",
    "SkillLicense",
    "SkillChecksum",
    "SkillProvenance",
    "SkillCompatibility",

    # Registries
    "SkillRegistry",
    "LocalSkillRegistry",
    "GitSkillRegistry",
    "GitHubSkillRegistry",

    # Discovery
    "LocalSkillDiscovery",
    "GitSkillDiscovery",
    "GitHubSkillDiscovery",
    "SkillDiscoveryOrchestrator",

    # Validation
    "SkillValidator",

    # Loading
    "DynamicSkillLoader",
    "SkillLoaderFactory",
    "SkillEntrypointResolver",

    # Versioning
    "VersionManager",
    "UpdateArchitecture",
    "CandidateValidator",
    "SmokeTestRunner",
]