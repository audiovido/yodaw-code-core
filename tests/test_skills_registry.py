"""
Tests for the Skill Registry.
"""

import pytest

from app.skills.models import Intent, Skill, SkillId, SkillInput, SkillPrerequisite, PlanningGuidance, ValidationGuidance, RiskLevel, EvidenceSchema
from app.skills.registry import SkillRegistry, SkillRegistration
from app.skills.skills import BugfixSkill, RefactorSkill, TestSkill


@pytest.fixture
def registry():
    """Create a fresh registry."""
    return SkillRegistry()


@pytest.fixture
def sample_skill():
    """Create a sample skill for testing."""
    return Skill(
        id="test-skill",
        name="Test Skill",
        description="A test skill",
        supported_intents=[Intent.TEST],
        inputs=[SkillInput("input1", "string", "Test input")],
        prerequisites=[SkillPrerequisite("tool1", "Test tool", "check_tool1")],
        planning_guidance=PlanningGuidance(
            typical_steps=["step1", "step2"],
            common_pitfalls=["pitfall1"],
            estimated_duration_minutes=30,
        ),
        validation_guidance=ValidationGuidance(
            success_criteria=["criteria1"],
            validation_commands=["cmd1"],
        ),
        risk=RiskLevel.LOW,
        evidence_schema=EvidenceSchema(
            required_fields=["field1"],
            optional_fields=["field2"],
        ),
        supported_languages=["python", "javascript"],
        tags=["test", "example"],
    )


class TestSkillRegistry:
    """Tests for SkillRegistry."""

    def test_register_skill(self, registry, sample_skill):
        """Test registering a skill."""
        skill_id = registry.register(sample_skill)
        assert skill_id == "test-skill"
        assert registry.count() == 1

    def test_get_skill(self, registry, sample_skill):
        """Test getting a skill by ID."""
        registry.register(sample_skill)
        skill = registry.get("test-skill")
        assert skill is not None
        assert skill.id == "test-skill"
        assert skill.name == "Test Skill"

    def test_get_nonexistent_skill(self, registry):
        """Test getting a non-existent skill returns None."""
        skill = registry.get("nonexistent")
        assert skill is None

    def test_unregister_skill(self, registry, sample_skill):
        """Test unregistering a skill."""
        registry.register(sample_skill)
        assert registry.unregister("test-skill") is True
        assert registry.count() == 0
        assert registry.get("test-skill") is None

    def test_unregister_nonexistent(self, registry):
        """Test unregistering non-existent skill returns False."""
        assert registry.unregister("nonexistent") is False

    def test_enable_disable_skill(self, registry, sample_skill):
        """Test enabling and disabling skills."""
        registry.register(sample_skill)
        assert registry.is_enabled("test-skill") is True

        registry.disable("test-skill")
        assert registry.is_enabled("test-skill") is False
        assert registry.count(enabled_only=True) == 0

        registry.enable("test-skill")
        assert registry.is_enabled("test-skill") is True
        assert registry.count(enabled_only=True) == 1

    def test_get_by_intent(self, registry, sample_skill):
        """Test getting skills by intent."""
        registry.register(sample_skill)
        skills = registry.get_by_intent(Intent.TEST)
        assert len(skills) == 1
        assert skills[0].id == "test-skill"

        skills = registry.get_by_intent(Intent.BUGFIX)
        assert len(skills) == 0

    def test_get_by_language(self, registry, sample_skill):
        """Test getting skills by language."""
        registry.register(sample_skill)
        skills = registry.get_by_language("python")
        assert len(skills) == 1

        skills = registry.get_by_language("rust")
        assert len(skills) == 0

    def test_get_by_tag(self, registry, sample_skill):
        """Test getting skills by tag."""
        registry.register(sample_skill)
        skills = registry.get_by_tag("test")
        assert len(skills) == 1

        skills = registry.get_by_tag("nonexistent")
        assert len(skills) == 0

    def test_filter_skills(self, registry):
        """Test filtering skills by multiple criteria."""
        # Register multiple skills
        bugfix = Skill(
            id="bugfix", name="Bugfix", description="Fix bugs",
            supported_intents=[Intent.BUGFIX],
            inputs=[], prerequisites=[],
            planning_guidance=PlanningGuidance([], [], 30),
            validation_guidance=ValidationGuidance([], []),
            risk=RiskLevel.MEDIUM,
            evidence_schema=EvidenceSchema([]),
            supported_languages=["python"],
            tags=["bugfix"],
        )
        refactor = Skill(
            id="refactor", name="Refactor", description="Refactor code",
            supported_intents=[Intent.REFACTOR],
            inputs=[], prerequisites=[],
            planning_guidance=PlanningGuidance([], [], 30),
            validation_guidance=ValidationGuidance([], []),
            risk=RiskLevel.LOW,
            evidence_schema=EvidenceSchema([]),
            supported_languages=["python", "javascript"],
            tags=["refactor"],
        )
        registry.register(bugfix)
        registry.register(refactor)

        # Filter by intent
        skills = registry.filter_skills(intent=Intent.BUGFIX)
        assert len(skills) == 1
        assert skills[0].id == "bugfix"

        # Filter by language
        skills = registry.filter_skills(language="javascript")
        assert len(skills) == 1
        assert skills[0].id == "refactor"

        # Filter by tags
        skills = registry.filter_skills(tags=["refactor"])
        assert len(skills) == 1
        assert skills[0].id == "refactor"

        # Filter by max risk
        skills = registry.filter_skills(max_risk=RiskLevel.LOW)
        assert len(skills) == 1
        assert skills[0].id == "refactor"

    def test_list_skill_ids(self, registry, sample_skill):
        """Test listing skill IDs."""
        registry.register(sample_skill)
        ids = registry.list_skill_ids()
        assert "test-skill" in ids

    def test_clear_registry(self, registry, sample_skill):
        """Test clearing the registry."""
        registry.register(sample_skill)
        registry.clear()
        assert registry.count() == 0

    def test_skill_factory(self, registry):
        """Test skill factory registration."""
        def factory(**kwargs):
            return {"custom": "instance", **kwargs}

        skill = Skill(
            id="factory-skill", name="Factory Skill", description="Test",
            supported_intents=[Intent.TEST],
            inputs=[], prerequisites=[],
            planning_guidance=PlanningGuidance([], [], 30),
            validation_guidance=ValidationGuidance([], []),
            risk=RiskLevel.LOW,
            evidence_schema=EvidenceSchema([]),
        )
        registry.register(skill, factory=factory)

        factory_fn = registry.get_skill_factory("factory-skill")
        assert factory_fn is not None
        instance = factory_fn(custom_arg="value")
        assert instance["custom"] == "instance"
        assert instance["custom_arg"] == "value"

    def test_create_skill_instance(self, registry):
        """Test creating skill instance via factory."""
        def factory(**kwargs):
            return Skill(
                id="created-skill", name="Created", description="Test",
                supported_intents=[Intent.TEST],
                inputs=[], prerequisites=[],
                planning_guidance=PlanningGuidance([], [], 30),
                validation_guidance=ValidationGuidance([], []),
                risk=RiskLevel.LOW,
                evidence_schema=EvidenceSchema([]),
            )

        skill = Skill(
            id="factory-skill", name="Factory Skill", description="Test",
            supported_intents=[Intent.TEST],
            inputs=[], prerequisites=[],
            planning_guidance=PlanningGuidance([], [], 30),
            validation_guidance=ValidationGuidance([], []),
            risk=RiskLevel.LOW,
            evidence_schema=EvidenceSchema([]),
        )
        registry.register(skill, factory=factory)

        instance = registry.create_skill_instance("factory-skill")
        assert instance.id == "created-skill"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])