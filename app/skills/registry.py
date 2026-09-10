"""
Skill registry: registration, discovery, ranking, and selection.
"""
from typing import Optional
from app.skills.protocols import Skill, SkillIntent, RiskLevel


class BugfixSkill:
    """Bugfix skill: reproduce, diagnose, fix, validate."""
    
    @property
    def skill_id(self) -> str:
        return "bugfix"
    
    @property
    def description(self) -> str:
        return "Fix bugs by reproducing failure, identifying root cause, applying minimal fix, and validating"
    
    @property
    def supported_intents(self) -> list:
        return [SkillIntent.BUGFIX]
    
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM
    
    def planning_guidance(self) -> str:
        return """
BUGFIX WORKFLOW:

1. Reproduce the failure (test, error output, or explicit reproduction steps)
2. Identify root cause from error messages, stack traces, and relevant code
3. Propose minimal fix targeting the root cause only
4. Apply fix
5. Rerun targeted test to confirm fix
6. Run broader validation to prevent regressions
7. Review diff for unintended changes
8. Commit only if all validation passes

EVIDENCE REQUIRED:
- reproduced failure (test output or error)
- root cause analysis
- fix description
- validation results

IMPORTANT:
- If failure cannot be reproduced, return action="blocked"
- Never invent a root cause without evidence
- Minimal fix: change only what is necessary
- Never weaken tests to make them pass
""".strip()
    
    def validation_guidance(self) -> str:
        return "Must reproduce original failure, then confirm fix passes targeted test and full test suite"
    
    def evidence_schema(self) -> dict:
        return {
            "reproduced": "bool",
            "root_cause": "str",
            "fix_applied": "bool",
            "tests_passed": "bool",
        }


class RefactorSkill:
    """Refactor skill: restructure without behavior change."""
    
    @property
    def skill_id(self) -> str:
        return "refactor"
    
    @property
    def description(self) -> str:
        return "Restructure code without changing observable behavior"
    
    @property
    def supported_intents(self) -> list:
        return [SkillIntent.REFACTOR]
    
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM
    
    def planning_guidance(self) -> str:
        return """
REFACTOR WORKFLOW:

1. Inspect existing behavior and tests
2. Identify refactor scope (files, functions, modules)
3. Preserve all public behavior and APIs
4. Apply refactoring edits
5. Run targeted validation on affected modules
6. Run full regression test suite
7. Review diff to confirm no behavior change
8. Commit only if green

EVIDENCE REQUIRED:
- scope identification
- behavior preservation confirmation
- validation results

IMPORTANT:
- Refactor must NOT change observable behavior unless explicitly requested
- All existing tests must continue to pass
- Public APIs must remain compatible
- Never remove tests during refactoring
""".strip()
    
    def validation_guidance(self) -> str:
        return "Full test suite must pass; behavior must be preserved"
    
    def evidence_schema(self) -> dict:
        return {
            "scope": "list[str]",
            "behavior_preserved": "bool",
            "tests_passed": "bool",
        }


class TestSkill:
    """Test skill: add missing tests, regression tests, edge cases."""
    
    @property
    def skill_id(self) -> str:
        return "test"
    
    @property
    def description(self) -> str:
        return "Add missing tests, regression tests for bugs, and edge case coverage"
    
    @property
    def supported_intents(self) -> list:
        return [SkillIntent.TEST]
    
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW
    
    def planning_guidance(self) -> str:
        return """
TEST WORKFLOW:

1. Identify testing gaps (uncovered functions, edge cases, regression scenarios)
2. Determine appropriate test type (unit, integration, edge case)
3. Write meaningful tests that verify actual behavior
4. Run tests to ensure they work
5. For regression tests: verify test fails before fix, passes after
6. Commit tests

EVIDENCE REQUIRED:
- test coverage gap identified
- tests added
- tests executed successfully

IMPORTANT:
- Never write fake green tests that always pass
- Regression tests should fail before the fix is applied
- Tests must be meaningful and verify actual behavior
- Avoid brittle tests that break on unrelated changes
""".strip()
    
    def validation_guidance(self) -> str:
        return "New tests must execute successfully and provide meaningful coverage"
    
    def evidence_schema(self) -> dict:
        return {
            "tests_added": "int",
            "test_type": "str",
            "all_tests_passed": "bool",
        }


class ReviewSkill:
    """Review skill: analyze code without modification."""
    
    @property
    def skill_id(self) -> str:
        return "review"
    
    @property
    def description(self) -> str:
        return "Review code for correctness, security, performance, and quality issues"
    
    @property
    def supported_intents(self) -> list:
        return [SkillIntent.REVIEW]
    
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW
    
    def planning_guidance(self) -> str:
        return """
REVIEW WORKFLOW:

1. Read code and identify review scope
2. Check for:
   - Correctness risks (logic errors, edge case handling)
   - Missing tests
   - Concurrency bugs (race conditions, deadlocks)
   - Error handling gaps
   - Security issues (injection, auth bypass, data exposure)
   - Performance problems (N+1 queries, memory leaks)
   - Dead code
   - API compatibility issues
3. Return structured findings with severity and locations
4. DO NOT modify code unless explicitly requested

EVIDENCE REQUIRED:
- files reviewed
- findings with severity (critical/high/medium/low)
- file/line references

IMPORTANT:
- Review mode is READ-ONLY by default
- Never auto-fix issues during review unless requested
- Provide actionable feedback with specific locations
""".strip()
    
    def validation_guidance(self) -> str:
        return "Review must not modify repository unless explicitly requested"
    
    def evidence_schema(self) -> dict:
        return {
            "files_reviewed": "list[str]",
            "findings": "list[dict]",
            "severity_counts": "dict",
        }


class FeatureSkill:
    """Feature skill: add new functionality with reuse-first approach."""
    
    @property
    def skill_id(self) -> str:
        return "feature"
    
    @property
    def description(self) -> str:
        return "Implement new features by reusing existing components and patterns"
    
    @property
    def supported_intents(self) -> list:
        return [SkillIntent.FEATURE]
    
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM
    
    def planning_guidance(self) -> str:
        return """
FEATURE WORKFLOW:

1. Inspect existing architecture and reusable components
2. Identify minimal integration points
3. Implement smallest coherent slice of functionality
4. Add tests for new behavior
5. Run validation
6. Review diff for unintended changes
7. Commit

EVIDENCE REQUIRED:
- reuse analysis (existing components identified)
- integration points
- tests added
- validation results

IMPORTANT:
- Reuse existing project code and patterns first
- Avoid broad rewrites or architectural changes
- Implement minimal slice that provides value
- Never skip tests for new features
""".strip()
    
    def validation_guidance(self) -> str:
        return "Feature must pass tests and integrate cleanly with existing code"
    
    def evidence_schema(self) -> dict:
        return {
            "reuse_components": "list[str]",
            "integration_points": "list[str]",
            "tests_added": "bool",
            "tests_passed": "bool",
        }


class DependencySkill:
    """Dependency skill: manage libraries with reuse and quality gates."""
    
    @property
    def skill_id(self) -> str:
        return "dependency"
    
    @property
    def description(self) -> str:
        return "Manage dependencies with reuse-first approach and quality gates"
    
    @property
    def supported_intents(self) -> list:
        return [SkillIntent.DEPENDENCY]
    
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.MEDIUM
    
    def planning_guidance(self) -> str:
        return """
DEPENDENCY WORKFLOW:

REUSE PRIORITY:
1. Existing project code
2. Existing installed dependency
3. Mature external library (quality gate required)
4. Custom implementation (last resort)

QUALITY GATE:
- License compatibility
- Active maintenance
- Project ecosystem fit
- Version compatibility
- Security track record

EVIDENCE REQUIRED:
- reuse analysis
- quality gate results
- dependency added/updated
- tests passed

IMPORTANT:
- Never install risky dependencies without explicit approval
- Check GitHub discovery and quality gate
- Prefer well-known, actively maintained packages
""".strip()
    
    def validation_guidance(self) -> str:
        return "Dependency must pass quality gate and not break existing tests"
    
    def evidence_schema(self) -> dict:
        return {
            "reuse_check": "dict",
            "quality_gate": "dict",
            "dependency_added": "str",
            "tests_passed": "bool",
        }


class DocumentationSkill:
    """Documentation skill: write docs, README, API docs."""
    
    @property
    def skill_id(self) -> str:
        return "documentation"
    
    @property
    def description(self) -> str:
        return "Write documentation that reflects actual behavior"
    
    @property
    def supported_intents(self) -> list:
        return [SkillIntent.DOCUMENTATION]
    
    @property
    def risk_level(self) -> RiskLevel:
        return RiskLevel.LOW
    
    def planning_guidance(self) -> str:
        return """
DOCUMENTATION WORKFLOW:

1. Identify documentation target (README, API docs, setup, changelog)
2. Inspect actual code behavior
3. Write documentation reflecting real behavior
4. Validate documentation accuracy
5. Commit

DOCUMENTATION TYPES:
- README: project overview, setup, usage
- API docs: function/class documentation
- Setup docs: installation, configuration
- Changelog: version history
- Migration notes: upgrade guides

IMPORTANT:
- Document real behavior, not aspirational claims
- Keep documentation concise and accurate
- Update examples to match current code
""".strip()
    
    def validation_guidance(self) -> str:
        return "Documentation must accurately reflect code behavior"
    
    def evidence_schema(self) -> dict:
        return {
            "doc_type": "str",
            "files_updated": "list[str]",
            "accuracy_verified": "bool",
        }


# Skill registry
_REGISTRY: dict[str, Skill] = {}


def register_skill(skill: Skill) -> None:
    """Register a skill in the global registry."""
    _REGISTRY[skill.skill_id] = skill


def get_skill(skill_id: str) -> Optional[Skill]:
    """Retrieve skill by ID."""
    return _REGISTRY.get(skill_id)


def list_skills() -> list:
    """List all registered skills."""
    return list(_REGISTRY.values())


def find_skills_for_intent(intent: SkillIntent) -> list:
    """Find all skills supporting a given intent."""
    return [
        skill
        for skill in _REGISTRY.values()
        if intent in skill.supported_intents
    ]


def select_skill(intent: SkillIntent) -> Optional[Skill]:
    """
    Select the best skill for a given intent.
    Returns None if no skill supports the intent.
    """
    candidates = find_skills_for_intent(intent)
    
    if not candidates:
        return None
    
    # Simple selection: first registered skill wins
    # More sophisticated ranking could consider risk, complexity, etc.
    return candidates[0]


# Auto-register all built-in skills
def _register_builtin_skills():
    register_skill(BugfixSkill())
    register_skill(RefactorSkill())
    register_skill(TestSkill())
    register_skill(ReviewSkill())
    register_skill(FeatureSkill())
    register_skill(DependencySkill())
    register_skill(DocumentationSkill())


_register_builtin_skills()
