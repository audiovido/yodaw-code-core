"""
Tests for Skill SDK Models.
"""

import pytest
from datetime import datetime
from app.skill_sdk.models import (
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


def test_skill_metadata_creation():
    """Test creating skill metadata."""
    metadata = SkillMetadata(
        name="Test Skill",
        description="A test skill",
        skill_id="test-skill-123",
        version="1.0.0",
        author="Test Author",
        license=SkillLicense.MIT,
        capabilities=[SkillCapability.ANALYSIS],
        languages=[SkillLanguage.PYTHON],
        risk_level=SkillRiskLevel.LOW,
        tags=["test", "sample"]
    )

    assert metadata.name == "Test Skill"
    assert metadata.description == "A test skill"
    assert metadata.skill_id == "test-skill-123"
    assert metadata.version == "1.0.0"
    assert metadata.author == "Test Author"
    assert metadata.license == SkillLicense.MIT
    assert SkillCapability.ANALYSIS in metadata.capabilities
    assert SkillLanguage.PYTHON in metadata.languages
    assert metadata.risk_level == SkillRiskLevel.LOW
    assert "test" in metadata.tags
    assert isinstance(metadata.created_at, datetime)
    assert isinstance(metadata.updated_at, datetime)


def test_skill_source_creation():
    """Test creating skill source."""
    source = SkillSource(
        type="local",
        url="/path/to/skill",
        branch="main",
        subdirectory="skills/test"
    )

    assert source.type == "local"
    assert source.url == "/path/to/skill"
    assert source.branch == "main"
    assert source.subdirectory == "skills/test"


def test_skill_manifest_creation():
    """Test creating a complete skill manifest."""
    metadata = SkillMetadata(
        name="Test Skill",
        description="A test skill for validation",
        skill_id="test-skill-456",
        version="2.1.0",
        license=SkillLicense.APACHE_2_0,
        capabilities=[SkillCapability.SYNTHESIS, SkillCapability.VALIDATION],
        languages=[SkillLanguage.PYTHON, SkillLanguage.JAVASCRIPT],
        risk_level=SkillRiskLevel.MEDIUM,
        tags=["validation", "testing", "validation"]
    )

    source = SkillSource(
        type="git",
        url="https://github.com/example/skill-repo",
        branch="develop",
        subdirectory="skills/validator"
    )

    manifest = SkillManifest(
        metadata=metadata,
        source=source,
        state=SkillState.DISCOVERED
    )

    assert manifest.metadata == metadata
    assert manifest.source == source
    assert manifest.state == SkillState.DISCOVERED
    assert manifest.version == "1.0.0"  # Default manifest version
    assert isinstance(manifest.last_checked, datetime)


def test_skill_manifest_serialization():
    """Test serializing and deserializing skill manifest."""
    metadata = SkillMetadata(
        name="Serialization Test",
        description="Testing manifest serialization",
        skill_id="serial-test-789",
        version="1.2.3",
        license=SkillLicense.GPL_3_0,
        capabilities=[SkillCapability.TRANSFORMATION],
        languages=[SkillLanguage.GO],
        risk_level=SkillRiskLevel.HIGH,
        tags=["serialization", "test"]
    )

    source = SkillSource(
        type="github",
        url="https://github.com/example/test-skill",
        branch="feature/new-feature",
        subdirectory="",
        token="ghp_testtoken123"
    )

    original_manifest = SkillManifest(
        metadata=metadata,
        source=source,
        dependencies=[SkillDependency("dep-skill", ">=1.0.0", False)],
        state=SkillState.VALIDATED
    )

    # Serialize to dict
    manifest_dict = original_manifest.to_dict()

    # Deserialize from dict
    restored_manifest = SkillManifest.from_dict(manifest_dict)

    # Check that key fields match
    assert restored_manifest.metadata.name == original_manifest.metadata.name
    assert restored_manifest.metadata.description == original_manifest.metadata.description
    assert restored_manifest.metadata.skill_id == original_manifest.metadata.skill_id
    assert restored_manifest.metadata.version == original_manifest.metadata.version
    assert restored_manifest.metadata.license == original_manifest.metadata.license
    assert restored_manifest.metadata.capabilities == original_manifest.metadata.capabilities
    assert restored_manifest.metadata.languages == original_manifest.metadata.languages
    assert restored_manifest.metadata.risk_level == original_manifest.metadata.risk_level
    assert restored_manifest.metadata.tags == original_manifest.metadata.tags

    assert restored_manifest.source.type == original_manifest.source.type
    assert restored_manifest.source.url == original_manifest.source.url
    assert restored_manifest.source.branch == original_manifest.source.branch
    assert restored_manifest.source.subdirectory == original_manifest.source.subdirectory
    assert restored_manifest.source.token == original_manifest.source.token

    assert len(restored_manifest.dependencies) == len(original_manifest.dependencies)
    assert restored_manifest.dependencies[0].skill_id == original_manifest.dependencies[0].skill_id
    assert restored_manifest.dependencies[0].version_constraint == original_manifest.dependencies[0].version_constraint
    assert restored_manifest.dependencies[0].optional == original_manifest.dependencies[0].optional

    assert restored_manifest.state == original_manifest.state


def test_skill_state_enum():
    """Test SkillState enum values."""
    assert SkillState.DISCOVERED.value == "discovered"
    assert SkillState.VALIDATED.value == "validated"
    assert SkillState.LOADED.value == "loaded"
    assert SkillState.ACTIVE.value == "active"
    assert SkillState.ERROR.value == "error"
    assert SkillState.DEPRECATED.value == "deprecated"


def test_skill_capability_enum():
    """Test SkillCapability enum values."""
    assert SkillCapability.ANALYSIS.value == "analysis"
    assert SkillCapability.SYNTHESIS.value == "synthesis"
    assert SkillCapability.TRANSFORMATION.value == "transformation"
    assert SkillCapability.VALIDATION.value == "validation"
    assert SkillCapability.OPTIMIZATION.value == "optimization"
    assert SkillCapability.SECURITY.value == "security"
    assert SkillCapability.DOCUMENTATION.value == "documentation"


def test_skill_language_enum():
    """Test SkillLanguage enum values."""
    assert SkillLanguage.PYTHON.value == "python"
    assert SkillLanguage.TYPESCRIPT.value == "typescript"
    assert SkillLanguage.JAVASCRIPT.value == "javascript"
    assert SkillLanguage.GO.value == "go"
    assert SkillLanguage.RUST.value == "rust"
    assert SkillLanguage.JAVA.value == "java"
    assert SkillLanguage.SWIFT.value == "swift"
    assert SkillLanguage.CPP.value == "cpp"
    assert SkillLanguage.CSHARP.value == "csharp"
    assert SkillLanguage.PHP.value == "php"
    assert SkillLanguage.RUBY.value == "ruby"


def test_skill_risk_level_enum():
    """Test SkillRiskLevel enum values."""
    assert SkillRiskLevel.LOW.value == "low"
    assert SkillRiskLevel.MEDIUM.value == "medium"
    assert SkillRiskLevel.HIGH.value == "high"
    assert SkillRiskLevel.CRITICAL.value == "critical"


def test_skill_license_enum():
    """Test SkillLicense enum values."""
    assert SkillLicense.MIT.value == "MIT"
    assert SkillLicense.APACHE_2_0.value == "Apache-2.0"
    assert SkillLicense.GPL_3_0.value == "GPL-3.0"
    assert SkillLicense.LGPL_3_0.value == "LGPL-3.0"
    assert SkillLicense.BSD_3_CLAUSE.value == "BSD-3-Clause"
    assert SkillLicense.ISC.value == "ISC"
    assert SkillLicense.PROPRIETARY.value == "Proprietary"
    assert SkillLicense.UNKNOWN.value == "UNKNOWN"


def test_skill_dependency_creation():
    """Test creating skill dependency."""
    dep = SkillDependency(
        skill_id="required-skill",
        version_constraint=">=2.0.0",
        optional=False
    )

    assert dep.skill_id == "required-skill"
    assert dep.version_constraint == ">=2.0.0"
    assert dep.optional is False

    # Test optional dependency
    optional_dep = SkillDependency(
        skill_id="optional-skill",
        version_constraint="",
        optional=True
    )

    assert optional_dep.skill_id == "optional-skill"
    assert optional_dep.version_constraint == ""
    assert optional_dep.optional is True


def test_skill_checksum_creation():
    """Test creating skill checksum."""
    checksum = SkillChecksum(
        algorithm="sha256",
        value="abc123def456"
    )

    assert checksum.algorithm == "sha256"
    assert checksum.value == "abc123def456"

    # Test serialization
    checksum_dict = checksum.to_dict()
    assert checksum_dict["algorithm"] == "sha256"
    assert checksum_dict["value"] == "abc123def456"

    # Test deserialization
    restored_checksum = SkillChecksum.from_dict(checksum_dict)
    assert restored_checksum.algorithm == checksum.algorithm
    assert restored_checksum.value == checksum.value


def test_skill_provenance_creation():
    """Test creating skill provenance."""
    prov = SkillProvenance(
        author="testuser",
        repository="https://github.com/testuser/test-repo",
        branch="feature-branch",
        commit="abc123def456"
    )

    assert prov.author == "testuser"
    assert prov.repository == "https://github.com/testuser/test-repo"
    assert prov.branch == "feature-branch"
    assert prov.commit == "abc123def456"
    assert isinstance(prov.created_at, datetime)

    # Test serialization
    prov_dict = prov.to_dict()
    assert prov_dict["author"] == "testuser"
    assert prov_dict["repository"] == "https://github.com/testuser/test-repo"
    assert prov_dict["branch"] == "feature-branch"
    assert prov_dict["commit"] == "abc123def456"
    assert "created_at" in prov_dict

    # Test deserialization
    restored_prov = SkillProvenance.from_dict(prov_dict)
    assert restored_prov.author == prov.author
    assert restored_prov.repository == prov.repository
    assert restored_prov.branch == prov.branch
    assert restored_prov.commit == prov.commit


def test_skill_compatibility_creation():
    """Test creating skill compatibility."""
    compat = SkillCompatibility(
        python_versions=["3.8", "3.9", "3.10"],
        platforms=["linux", "darwin", "win32"],
        dependencies=[
            SkillDependency("dep1", ">=1.0.0", False),
            SkillDependency("dep2", ">=2.0.0", True)
        ]
    )

    assert compat.python_versions == ["3.8", "3.9", "3.10"]
    assert compat.platforms == ["linux", "darwin", "win32"]
    assert len(compat.dependencies) == 2
    assert compat.dependencies[0].skill_id == "dep1"
    assert compat.dependencies[0].version_constraint == ">=1.0.0"
    assert compat.dependencies[0].optional is False
    assert compat.dependencies[1].skill_id == "dep2"
    assert compat.dependencies[1].version_constraint == ">=2.0.0"
    assert compat.dependencies[1].optional is True

    # Test serialization
    compat_dict = compat.to_dict()
    assert compat_dict["python_versions"] == ["3.8", "3.9", "3.10"]
    assert compat_dict["platforms"] == ["linux", "darwin", "win32"]
    assert len(compat_dict["dependencies"]) == 2

    # Test deserialization
    restored_compat = SkillCompatibility.from_dict(compat_dict)
    assert restored_compat.python_versions == compat.python_versions
    assert restored_compat.platforms == compat.platforms
    assert len(restored_compat.dependencies) == len(compat.dependencies)
    assert restored_compat.dependencies[0].skill_id == compat.dependencies[0].skill_id
    assert restored_compat.dependencies[0].version_constraint == compat.dependencies[0].version_constraint
    assert restored_compat.dependencies[0].optional == compat.dependencies[0].optional


if __name__ == "__main__":
    pytest.main([__file__, "-v"])