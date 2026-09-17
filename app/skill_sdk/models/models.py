"""
Skill SDK Models.

Defines the core data structures for skill metadata, manifest, and related entities.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class SkillState(str, Enum):
    """Skill lifecycle state."""
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    LOADED = "loaded"
    ACTIVE = "active"
    ERROR = "error"
    DEPRECATED = "deprecated"


class SkillDependency:
    """Represents a skill dependency."""

    def __init__(
        self,
        skill_id: str,
        version_constraint: str = "",
        optional: bool = False,
    ):
        self.skill_id = skill_id
        self.version_constraint = version_constraint
        self.optional = optional

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version_constraint": self.version_constraint,
            "optional": self.optional,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillDependency":
        return cls(
            skill_id=data["skill_id"],
            version_constraint=data.get("version_constraint", ""),
            optional=data.get("optional", False),
        )


class SkillCapability(str, Enum):
    """Skill capability categories."""
    ANALYSIS = "analysis"
    SYNTHESIS = "synthesis"
    TRANSFORMATION = "transformation"
    VALIDATION = "validation"
    OPTIMIZATION = "optimization"
    SECURITY = "security"
    DOCUMENTATION = "documentation"


class SkillLanguage(str, Enum):
    """Supported programming languages."""
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    GO = "go"
    RUST = "rust"
    JAVA = "java"
    SWIFT = "swift"
    CPP = "cpp"
    CSHARP = "csharp"
    PHP = "php"
    RUBY = "ruby"


class SkillRiskLevel(str, Enum):
    """Risk levels for skill execution."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SkillLicense(str, Enum):
    """Software licenses."""
    MIT = "MIT"
    APACHE_2_0 = "Apache-2.0"
    GPL_3_0 = "GPL-3.0"
    LGPL_3_0 = "LGPL-3.0"
    BSD_3_CLAUSE = "BSD-3-Clause"
    ISC = "ISC"
    PROPRIETARY = "Proprietary"
    UNKNOWN = "UNKNOWN"


class SkillChecksum:
    """File checksum for integrity verification."""

    def __init__(self, algorithm: str, value: str):
        self.algorithm = algorithm
        self.value = value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "algorithm": self.algorithm,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillChecksum":
        return cls(
            algorithm=data["algorithm"],
            value=data["value"],
        )


class SkillProvenance:
    """Skill provenance and provenance metadata."""

    def __init__(
        self,
        author: str,
        repository: str,
        branch: str = "main",
        commit: str = "",
        created_at: Optional[datetime] = None,
    ):
        self.author = author
        self.repository = repository
        self.branch = branch
        self.commit = commit
        self.created_at = created_at or datetime.utcnow()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "author": self.author,
            "repository": self.repository,
            "branch": self.branch,
            "commit": self.commit,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillProvenance":
        return cls(
            author=data["author"],
            repository=data["repository"],
            branch=data.get("branch", "main"),
            commit=data.get("commit", ""),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
        )


class SkillCompatibility:
    """Skill compatibility information."""

    def __init__(
        self,
        python_versions: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None,
        dependencies: Optional[List[SkillDependency]] = None,
    ):
        self.python_versions = python_versions or []
        self.platforms = platforms or []
        self.dependencies = dependencies or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "python_versions": self.python_versions,
            "platforms": self.platforms,
            "dependencies": [dep.to_dict() for dep in self.dependencies],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillCompatibility":
        return cls(
            python_versions=data.get("python_versions", []),
            platforms=data.get("platforms", []),
            dependencies=[SkillDependency.from_dict(dep) for dep in data.get("dependencies", [])],
        )


@dataclass
class SkillMetadata:
    """Basic skill metadata."""

    name: str
    description: str
    version: str = "1.0.0"
    skill_id: str = field(default_factory=lambda: str(uuid4()))
    author: str = ""
    homepage: str = ""
    documentation: str = ""
    repository: str = ""
    entrypoint: Optional[str] = None
    license: SkillLicense = SkillLicense.UNKNOWN
    keywords: List[str] = field(default_factory=list)
    capabilities: List[SkillCapability] = field(default_factory=list)
    languages: List[SkillLanguage] = field(default_factory=list)
    risk_level: SkillRiskLevel = SkillRiskLevel.MEDIUM
    tags: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "skill_id": self.skill_id,
            "author": self.author,
            "homepage": self.homepage,
            "documentation": self.documentation,
            "repository": self.repository,
            "entrypoint": self.entrypoint,
            "license": self.license.value,
            "keywords": self.keywords,
            "capabilities": [cap.value for cap in self.capabilities],
            "languages": [lang.value for lang in self.languages],
            "risk_level": self.risk_level.value,
            "tags": self.tags,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillMetadata":
        return cls(
            name=data["name"],
            description=data["description"],
            version=data.get("version", "1.0.0"),
            skill_id=data.get("skill_id", str(uuid4())),
            author=data.get("author", ""),
            homepage=data.get("homepage", ""),
            documentation=data.get("documentation", ""),
            repository=data.get("repository", ""),
            entrypoint=data.get("entrypoint"),
            license=SkillLicense(data.get("license", "UNKNOWN")),
            keywords=data.get("keywords", []),
            capabilities=[SkillCapability(cap) for cap in data.get("capabilities", [])],
            languages=[SkillLanguage(lang) for lang in data.get("languages", [])],
            risk_level=(
                data["risk_level"]
                if isinstance(data.get("risk_level"), SkillRiskLevel)
                else SkillRiskLevel(str(data.get("risk_level", "medium")).lower())
            ),
            tags=data.get("tags", []),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.utcnow(),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else datetime.utcnow(),
        )


@dataclass
class SkillSource:
    """Skill source location and access information."""

    type: str  # local, git, github, etc.
    url: str
    branch: str = "main"
    subdirectory: str = ""
    token: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    entrypoint: Optional[str] = None

    def to_dict(self, include_credentials: bool = True) -> Dict[str, Any]:
        data = {
            "type": self.type,
            "url": self.url,
            "branch": self.branch,
            "subdirectory": self.subdirectory,
            "entrypoint": self.entrypoint,
        }
        if include_credentials:
            data.update(
                token=self.token,
                username=self.username,
                password=self.password,
            )
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillSource":
        return cls(
            type=data["type"],
            url=data["url"],
            branch=data.get("branch", "main"),
            subdirectory=data.get("subdirectory", ""),
            token=data.get("token"),
            username=data.get("username"),
            password=data.get("password"),
            entrypoint=data.get("entrypoint"),
        )


@dataclass
class SkillManifest:
    """Complete skill manifest."""

    metadata: SkillMetadata
    source: SkillSource
    dependencies: List[SkillDependency] = field(default_factory=list)
    compatibility: SkillCompatibility = field(default_factory=SkillCompatibility)
    provenance: SkillProvenance = field(default_factory=lambda: SkillProvenance(author="", repository=""))
    checksums: List[SkillChecksum] = field(default_factory=list)
    state: SkillState = SkillState.DISCOVERED
    error_message: Optional[str] = None
    error_details: Optional[Dict[str, Any]] = None
    last_checked: datetime = field(default_factory=datetime.utcnow)
    version: str = "1.0.0"  # Manifest version

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "source": self.source.to_dict(),
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "compatibility": self.compatibility.to_dict(),
            "provenance": self.provenance.to_dict(),
            "checksums": [cs.to_dict() for cs in self.checksums],
            "state": self.state.value,
            "error_message": self.error_message,
            "error_details": self.error_details,
            "last_checked": self.last_checked.isoformat(),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SkillManifest":
        return cls(
            metadata=SkillMetadata.from_dict(data["metadata"]),
            source=SkillSource.from_dict(data["source"]),
            dependencies=[SkillDependency.from_dict(dep) for dep in data.get("dependencies", [])],
            compatibility=SkillCompatibility.from_dict(data.get("compatibility", {})),
            provenance=SkillProvenance.from_dict(data.get("provenance", {"author": "", "repository": ""})),
            checksums=[SkillChecksum.from_dict(cs) for cs in data.get("checksums", [])],
            state=SkillState(data.get("state", "discovered")),
            error_message=data.get("error_message"),
            error_details=data.get("error_details"),
            last_checked=datetime.fromisoformat(data["last_checked"]) if data.get("last_checked") else datetime.utcnow(),
            version=data.get("version", "1.0.0"),
        )