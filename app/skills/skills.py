"""
Concrete skill implementations for YODAW Coder Skills.

Each skill implements a specific software engineering capability with
structured inputs, prerequisites, planning guidance, and validation.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from .models import (
    Skill,
    SkillId,
    Intent,
    SkillInput,
    SkillPrerequisite,
    PlanningGuidance,
    ValidationGuidance,
    RiskLevel,
    EvidenceSchema,
    SkillEvidence,
)


class BaseSkill:
    """Base class for all skills."""

    def create_skill(self) -> Skill:
        """Create the skill definition. Override in subclasses."""
        raise NotImplementedError

    def validate_inputs(self, inputs: dict[str, Any]) -> list[str]:
        """Validate inputs for this skill. Return list of errors."""
        return []

    def execute(self, inputs: dict[str, Any], context: dict[str, Any]) -> SkillEvidence:
        """Execute the skill. Override in subclasses."""
        raise NotImplementedError

    def validate_outputs(self, evidence: SkillEvidence) -> list[str]:
        """Validate outputs. Return list of errors."""
        return []


class BugfixSkill(BaseSkill):
    """Skill for fixing bugs with structured reproduce-diagnose-fix-verify loop."""

    def create_skill(self) -> Skill:
        return Skill(
            id="bugfix",
            name="Bug Fix",
            description="Systematic bug fixing with reproduce, diagnose, minimal fix, and verification loop",
            supported_intents=[Intent.BUGFIX],
            inputs=[
                SkillInput("bug_description", "string", "Description of the bug behavior", required=True),
                SkillInput("reproduction_steps", "list[string]", "Steps to reproduce the bug", required=True),
                SkillInput("expected_behavior", "string", "What should happen instead", required=True),
                SkillInput("actual_behavior", "string", "What actually happens", required=True),
                SkillInput("environment", "dict", "Environment details (OS, version, deps)", required=False, default={}),
                SkillInput("related_files", "list[string]", "Files potentially related to the bug", required=False, default=[]),
                SkillInput("error_logs", "string", "Error messages or stack traces", required=False, default=""),
            ],
            prerequisites=[
                SkillPrerequisite("test_framework", "Test framework available", "check_test_framework"),
                SkillPrerequisite("version_control", "Git repository", "check_git_repo"),
                SkillPrerequisite("debug_tools", "Debugging tools available", "check_debug_tools"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Create minimal reproduction case",
                    "Identify root cause through debugging",
                    "Implement minimal fix targeting root cause",
                    "Write targeted regression tests",
                    "Run full test suite for regressions",
                    "Review diff and commit",
                ],
                common_pitfalls=[
                    "Fixing symptoms instead of root cause",
                    "Insufficient reproduction case",
                    "Missing regression tests",
                    "Breaking unrelated functionality",
                    "Over-engineering the fix",
                ],
                estimated_duration_minutes=120,
                requires_human_review=True,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "Bug reproduction case passes (fails before fix, passes after)",
                    "Root cause identified and documented",
                    "Fix is minimal and targeted",
                    "All existing tests pass",
                    "New regression tests added",
                    "No new warnings or errors introduced",
                ],
                validation_commands=[
                    "python -m pytest -q",
                    "python -m pytest <repro_test> -v",
                    "git diff --stat",
                ],
                rollback_guidance="git stash && git checkout -- . && run full test suite",
            ),
            risk=RiskLevel.MEDIUM,
            evidence_schema=EvidenceSchema(
                required_fields=["reproduction_case", "root_cause", "fix_description", "test_results"],
                optional_fields=["performance_impact", "related_issues"],
                artifact_types=["test_file", "patch_file", "log_file"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["bugfix", "debugging", "regression-testing"],
        )


class RefactorSkill(BaseSkill):
    """Skill for refactoring code while preserving behavior."""

    def create_skill(self) -> Skill:
        return Skill(
            id="refactor",
            name="Refactor",
            description="Behavior-preserving code restructuring with comprehensive verification",
            supported_intents=[Intent.REFACTOR],
            inputs=[
                SkillInput("target_code", "string", "Code or files to refactor", required=True),
                SkillInput("refactoring_goal", "string", "Goal (simplify, extract, inline, rename, etc.)", required=True),
                SkillInput("preserve_behavior", "boolean", "Must preserve exact behavior", required=True, default=True),
                SkillInput("test_coverage_threshold", "float", "Minimum test coverage before refactor", required=False, default=0.8),
                SkillInput("scope", "string", "Scope: function, class, module, architecture", required=False, default="module"),
            ],
            prerequisites=[
                SkillPrerequisite("test_suite", "Comprehensive test suite exists", "check_test_suite"),
                SkillPrerequisite("version_control", "Git repository for rollback", "check_git_repo"),
                SkillPrerequisite("static_analysis", "Static analysis tools available", "check_static_analysis"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Analyze current structure and identify improvement areas",
                    "Verify test coverage meets threshold",
                    "Create detailed refactoring plan",
                    "Execute in small, verified increments",
                    "Run tests after each increment",
                    "Verify behavior preservation",
                    "Review and commit",
                ],
                common_pitfalls=[
                    "Insufficient test coverage before starting",
                    "Making too many changes at once",
                    "Changing behavior unintentionally",
                    "Not running tests frequently enough",
                    "Skipping edge case verification",
                ],
                estimated_duration_minutes=180,
                requires_human_review=True,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "All tests pass before and after",
                    "Behavior identical (verified by tests)",
                    "Code quality metrics improved",
                    "No new complexity introduced",
                    "Documentation updated if needed",
                ],
                validation_commands=[
                    "python -m pytest -q",
                    "python -m pytest --cov --cov-fail-under=80",
                    "git diff --stat",
                    "# Run behavior comparison tests",
                ],
                rollback_guidance="git reset --hard HEAD && run full test suite",
            ),
            risk=RiskLevel.MEDIUM,
            evidence_schema=EvidenceSchema(
                required_fields=["before_metrics", "after_metrics", "test_results", "behavior_verification"],
                optional_fields=["complexity_delta", "performance_delta"],
                artifact_types=["metrics_report", "test_results", "patch_file"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["refactor", "code-quality", "technical-debt"],
        )


class TestSkill(BaseSkill):
    """Skill for writing and maintaining tests."""

    def create_skill(self) -> Skill:
        return Skill(
            id="test",
            name="Test Writing",
            description="Comprehensive test creation covering happy paths, edge cases, and error conditions",
            supported_intents=[Intent.TEST],
            inputs=[
                SkillInput("target_code", "string", "Code to test", required=True),
                SkillInput("test_types", "list[string]", "Types: unit, integration, e2e, property", required=False, default=["unit"]),
                SkillInput("coverage_target", "float", "Target coverage percentage", required=False, default=0.8),
                SkillInput("framework", "string", "Test framework (pytest, jest, go test, etc.)", required=False, default="auto"),
                SkillInput("existing_tests", "string", "Path to existing tests for style reference", required=False, default=""),
            ],
            prerequisites=[
                SkillPrerequisite("test_framework", "Test framework configured", "check_test_framework"),
                SkillPrerequisite("source_access", "Access to source code", "check_source_access"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Analyze code to understand testing requirements",
                    "Design test cases (happy path, edge cases, errors)",
                    "Set up fixtures and mocks",
                    "Implement test cases",
                    "Run tests and verify they pass",
                    "Measure and improve coverage",
                ],
                common_pitfalls=[
                    "Testing implementation details instead of behavior",
                    "Missing edge cases and error conditions",
                    "Brittle tests that break on refactoring",
                    "Insufficient mock isolation",
                    "Not testing error paths",
                ],
                estimated_duration_minutes=90,
                requires_human_review=False,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "All new tests pass",
                    "Coverage meets or exceeds target",
                    "Tests follow project conventions",
                    "Tests are deterministic and fast",
                    "Edge cases and error paths covered",
                ],
                validation_commands=[
                    "python -m pytest -q",
                    "python -m pytest --cov --cov-report=term-missing",
                ],
                rollback_guidance="git checkout -- test files",
            ),
            risk=RiskLevel.LOW,
            evidence_schema=EvidenceSchema(
                required_fields=["test_files_created", "coverage_report", "test_results"],
                optional_fields=["mutation_score", "flakiness_check"],
                artifact_types=["test_file", "coverage_report", "test_results"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["testing", "unit-test", "integration-test", "coverage"],
        )


class ReviewSkill(BaseSkill):
    """Skill for code review and analysis."""

    def create_skill(self) -> Skill:
        return Skill(
            id="review",
            name="Code Review",
            description="Comprehensive code review with severity-rated findings",
            supported_intents=[Intent.REVIEW],
            inputs=[
                SkillInput("target", "string", "Code, files, or PR to review", required=True),
                SkillInput("review_type", "string", "Type: security, performance, style, architecture, general", required=False, default="general"),
                SkillInput("severity_threshold", "string", "Minimum severity: critical, high, medium, low", required=False, default="medium"),
                SkillInput("standards", "list[string]", "Coding standards to check against", required=False, default=[]),
                SkillInput("read_only", "boolean", "Read-only review (no changes)", required=True, default=True),
            ],
            prerequisites=[
                SkillPrerequisite("static_analysis", "Static analysis tools", "check_static_analysis"),
                SkillPrerequisite("source_access", "Access to source code", "check_source_access"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Define review scope and criteria",
                    "Run static analysis tools",
                    "Manual code review for logic and design",
                    "Document findings with severity ratings",
                    "Generate review report",
                    "Discuss critical findings if needed",
                ],
                common_pitfalls=[
                    "Focusing only on style, missing logic bugs",
                    "Inconsistent severity ratings",
                    "Not checking for security issues",
                    "Missing architectural concerns",
                    "Not providing actionable recommendations",
                ],
                estimated_duration_minutes=60,
                requires_human_review=False,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "All findings have severity ratings",
                    "Critical/high findings have clear reproduction steps",
                    "Recommendations are actionable",
                    "Report is clear and well-structured",
                ],
                validation_commands=[
                    "# Review report completeness",
                    "# Verify severity ratings",
                ],
                rollback_guidance="N/A - read-only skill",
            ),
            risk=RiskLevel.LOW,
            evidence_schema=EvidenceSchema(
                required_fields=["findings", "severity_ratings", "recommendations", "summary"],
                optional_fields=["metrics", "tool_outputs"],
                artifact_types=["review_report", "findings_list"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["review", "code-review", "static-analysis", "security-audit"],
        )


class FeatureSkill(BaseSkill):
    """Skill for implementing new features."""

    def create_skill(self) -> Skill:
        return Skill(
            id="feature",
            name="Feature Implementation",
            description="End-to-end feature implementation from requirements to deployment",
            supported_intents=[Intent.FEATURE],
            inputs=[
                SkillInput("requirements", "string", "Feature requirements and acceptance criteria", required=True),
                SkillInput("design_doc", "string", "Technical design document (optional)", required=False, default=""),
                SkillInput("api_contracts", "dict", "API contracts and schemas", required=False, default={}),
                SkillInput("integration_points", "list[string]", "Systems to integrate with", required=False, default=[]),
                SkillInput("test_strategy", "string", "Testing approach", required=False, default="comprehensive"),
            ],
            prerequisites=[
                SkillPrerequisite("requirements_clarity", "Requirements are clear and testable", "check_requirements"),
                SkillPrerequisite("environment", "Development environment ready", "check_environment"),
                SkillPrerequisite("dependencies", "Required dependencies available", "check_dependencies"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Analyze and clarify requirements",
                    "Create technical design",
                    "Implement in incremental steps",
                    "Write tests for each increment",
                    "Integrate and test end-to-end",
                    "Update documentation",
                    "Code review and commit",
                ],
                common_pitfalls=[
                    "Unclear or changing requirements",
                    "Skipping design phase",
                    "Insufficient testing",
                    "Not considering edge cases",
                    "Ignoring integration complexity",
                    "Missing documentation",
                ],
                estimated_duration_minutes=240,
                requires_human_review=True,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "All acceptance criteria met",
                    "Tests pass (unit, integration, e2e)",
                    "Code follows project conventions",
                    "Documentation updated",
                    "No performance regressions",
                    "Security considerations addressed",
                ],
                validation_commands=[
                    "python -m pytest -q",
                    "python -m pytest --cov",
                    "# Run integration tests",
                    "# Run performance benchmarks",
                ],
                rollback_guidance="git revert <commit> && verify rollback",
            ),
            risk=RiskLevel.MEDIUM,
            evidence_schema=EvidenceSchema(
                required_fields=["implementation_summary", "test_results", "api_docs", "integration_verification"],
                optional_fields=["performance_metrics", "security_notes"],
                artifact_types=["source_files", "test_files", "doc_files", "patch_file"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["feature", "implementation", "development"],
        )


class DependencySkill(BaseSkill):
    """Skill for managing dependencies - reuse, upgrade, audit."""

    def create_skill(self) -> Skill:
        return Skill(
            id="dependency",
            name="Dependency Management",
            description="Analyze, upgrade, audit, and manage dependencies with reuse-first approach",
            supported_intents=[Intent.DEPENDENCY],
            inputs=[
                SkillInput("action", "string", "Action: audit, upgrade, add, remove, reuse", required=True),
                SkillInput("target_dependencies", "list[string]", "Specific dependencies to target", required=False, default=[]),
                SkillInput("constraints", "dict", "Version constraints, licensing, security", required=False, default={}),
                SkillInput("prefer_reuse", "boolean", "Prefer existing internal code over new deps", required=True, default=True),
            ],
            prerequisites=[
                SkillPrerequisite("package_manager", "Package manager available", "check_package_manager"),
                SkillPrerequisite("lock_file", "Lock file exists", "check_lock_file"),
                SkillPrerequisite("security_scanner", "Security scanner available", "check_security_scanner"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Audit current dependencies",
                    "Check for vulnerabilities and outdated packages",
                    "Research alternatives (internal first, then external)",
                    "Create upgrade/migration plan with rollback",
                    "Execute changes incrementally",
                    "Verify functionality and security",
                ],
                common_pitfalls=[
                    "Adding dependencies without checking existing solutions",
                    "Upgrading without testing breaking changes",
                    "Ignoring license compatibility",
                    "Not updating lock files",
                    "Missing transitive dependency issues",
                ],
                estimated_duration_minutes=90,
                requires_human_review=True,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "No new vulnerabilities introduced",
                    "All tests pass after changes",
                    "Lock files updated correctly",
                    "Internal reuse prioritized",
                    "License compliance maintained",
                ],
                validation_commands=[
                    "python -m pytest -q",
                    "# Run security scan",
                    "# Verify lock file integrity",
                ],
                rollback_guidance="Restore lock file and reinstall dependencies",
            ),
            risk=RiskLevel.MEDIUM,
            evidence_schema=EvidenceSchema(
                required_fields=["audit_report", "changes_made", "test_results", "security_scan"],
                optional_fields=["license_report", "size_impact"],
                artifact_types=["audit_report", "lock_file", "patch_file"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["dependency", "package-management", "security", "reuse"],
        )


class DocumentationSkill(BaseSkill):
    """Skill for creating and maintaining documentation."""

    def create_skill(self) -> Skill:
        return Skill(
            id="documentation",
            name="Documentation",
            description="Create and maintain comprehensive documentation",
            supported_intents=[Intent.DOCUMENTATION],
            inputs=[
                SkillInput("doc_type", "string", "Type: api, guide, tutorial, readme, architecture, changelog", required=True),
                SkillInput("target_audience", "string", "Audience: developers, users, operators", required=False, default="developers"),
                SkillInput("source_code", "string", "Source code to document", required=False, default=""),
                SkillInput("existing_docs", "string", "Path to existing documentation", required=False, default=""),
                SkillInput("format", "string", "Format: markdown, rst, openapi, etc.", required=False, default="markdown"),
            ],
            prerequisites=[
                SkillPrerequisite("source_access", "Access to source code", "check_source_access"),
                SkillPrerequisite("doc_tools", "Documentation tools available", "check_doc_tools"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Audit current documentation gaps",
                    "Plan documentation structure",
                    "Write content with examples",
                    "Review for accuracy and clarity",
                    "Publish or commit",
                ],
                common_pitfalls=[
                    "Documentation drifts from code",
                    "Missing examples and use cases",
                    "Inconsistent style and terminology",
                    "Not updating after code changes",
                    "Over-documenting obvious things",
                ],
                estimated_duration_minutes=120,
                requires_human_review=False,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "Documentation builds without errors",
                    "Examples are tested and working",
                    "Cross-references are valid",
                    "Style guide followed",
                    "Covers all public APIs",
                ],
                validation_commands=[
                    "# Build documentation",
                    "# Check for broken links",
                    "# Validate examples",
                ],
                rollback_guidance="git checkout -- docs",
            ),
            risk=RiskLevel.LOW,
            evidence_schema=EvidenceSchema(
                required_fields=["doc_files_created", "coverage_report", "validation_results"],
                optional_fields=["example_test_results"],
                artifact_types=["doc_file", "coverage_report"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["documentation", "docs", "technical-writing"],
        )


class MigrationSkill(BaseSkill):
    """Skill for code migrations and upgrades."""

    def create_skill(self) -> Skill:
        return Skill(
            id="migration",
            name="Migration",
            description="Safe migration of code, data, or infrastructure with rollback plans",
            supported_intents=[Intent.MIGRATION],
            inputs=[
                SkillInput("migration_type", "string", "Type: framework, language, database, architecture, version", required=True),
                SkillInput("source_version", "string", "Current version", required=True),
                SkillInput("target_version", "string", "Target version", required=True),
                SkillInput("scope", "list[string]", "Components to migrate", required=True),
                SkillInput("data_migration", "boolean", "Whether data migration needed", required=False, default=False),
            ],
            prerequisites=[
                SkillPrerequisite("backup_strategy", "Backup and rollback plan", "check_backup"),
                SkillPrerequisite("test_environment", "Staging environment for testing", "check_staging"),
                SkillPrerequisite("migration_tools", "Migration tools available", "check_migration_tools"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Assess current state and scope",
                    "Create detailed migration plan",
                    "Prepare environment and backups",
                    "Execute in phases with verification",
                    "Validate completeness",
                    "Clean up legacy artifacts",
                ],
                common_pitfalls=[
                    "Insufficient testing before production",
                    "No rollback plan",
                    "Underestimating data migration complexity",
                    "Missing dependency updates",
                    "Not validating edge cases",
                ],
                estimated_duration_minutes=360,
                requires_human_review=True,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "All components migrated successfully",
                    "Data integrity verified",
                    "All tests pass",
                    "Performance meets baseline",
                    "Rollback tested and documented",
                ],
                validation_commands=[
                    "python -m pytest -q",
                    "# Run data integrity checks",
                    "# Run performance benchmarks",
                ],
                rollback_guidance="Execute documented rollback procedure",
            ),
            risk=RiskLevel.HIGH,
            evidence_schema=EvidenceSchema(
                required_fields=["migration_plan", "phase_results", "validation_results", "rollback_test"],
                optional_fields=["performance_comparison", "issue_log"],
                artifact_types=["migration_plan", "validation_report", "patch_files"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["migration", "upgrade", "modernization"],
        )


class PerformanceSkill(BaseSkill):
    """Skill for performance optimization."""

    def create_skill(self) -> Skill:
        return Skill(
            id="performance",
            name="Performance Optimization",
            description="Systematic performance analysis and optimization with benchmarking",
            supported_intents=[Intent.PERFORMANCE],
            inputs=[
                SkillInput("target", "string", "Component or system to optimize", required=True),
                SkillInput("baseline_metrics", "dict", "Current performance metrics", required=False, default={}),
                SkillInput("target_metrics", "dict", "Target performance goals", required=False, default={}),
                SkillInput("constraints", "dict", "Constraints: memory, cpu, latency, cost", required=False, default={}),
                SkillInput("profiling_data", "string", "Existing profiling data", required=False, default=""),
            ],
            prerequisites=[
                SkillPrerequisite("profiling_tools", "Profiling tools available", "check_profiling_tools"),
                SkillPrerequisite("benchmark_suite", "Benchmark suite exists", "check_benchmarks"),
                SkillPrerequisite("load_test", "Load testing capability", "check_load_test"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Establish baseline metrics",
                    "Profile to identify bottlenecks",
                    "Analyze and prioritize optimizations",
                    "Implement optimizations incrementally",
                    "Benchmark after each change",
                    "Verify no regressions",
                ],
                common_pitfalls=[
                    "Optimizing without profiling",
                    "Premature optimization",
                    "Introducing bugs during optimization",
                    "Not measuring real-world workloads",
                    "Ignoring trade-offs (memory vs CPU)",
                ],
                estimated_duration_minutes=180,
                requires_human_review=True,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "Measurable improvement in target metrics",
                    "No functional regressions",
                    "No new bottlenecks introduced",
                    "Optimizations are maintainable",
                ],
                validation_commands=[
                    "# Run benchmarks",
                    "python -m pytest -q",
                    "# Compare before/after metrics",
                ],
                rollback_guidance="git revert optimization commits",
            ),
            risk=RiskLevel.MEDIUM,
            evidence_schema=EvidenceSchema(
                required_fields=["baseline_metrics", "optimized_metrics", "bottleneck_analysis", "changes_made"],
                optional_fields=["profiling_data", "tradeoff_analysis"],
                artifact_types=["benchmark_report", "profiling_data", "patch_file"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["performance", "optimization", "benchmarking", "profiling"],
        )


class SecuritySkill(BaseSkill):
    """Skill for security hardening and vulnerability remediation."""

    def create_skill(self) -> Skill:
        return Skill(
            id="security",
            name="Security Hardening",
            description="Security vulnerability assessment, remediation, and hardening",
            supported_intents=[Intent.SECURITY],
            inputs=[
                SkillInput("scan_results", "string", "Security scan results (SAST, DAST, SCA)", required=False, default=""),
                SkillInput("target_scope", "string", "Scope: application, infrastructure, dependencies", required=True),
                SkillInput("compliance_standards", "list[string]", "Standards: OWASP, NIST, PCI-DSS, etc.", required=False, default=[]),
                SkillInput("threat_model", "string", "Threat model document", required=False, default=""),
            ],
            prerequisites=[
                SkillPrerequisite("security_scanners", "Security scanning tools", "check_security_scanners"),
                SkillPrerequisite("secret_scanner", "Secret detection tools", "check_secret_scanner"),
                SkillPrerequisite("dependency_scanner", "Dependency vulnerability scanner", "check_dependency_scanner"),
            ],
            planning_guidance=PlanningGuidance(
                typical_steps=[
                    "Run comprehensive security scans",
                    "Analyze and triage findings",
                    "Create remediation plan prioritized by risk",
                    "Implement fixes",
                    "Re-scan to verify",
                    "Update security documentation",
                ],
                common_pitfalls=[
                    "Ignoring low-severity findings that chain",
                    "Fixing symptoms not root causes",
                    "Introducing new vulnerabilities",
                    "Not updating threat model",
                    "Missing configuration issues",
                ],
                estimated_duration_minutes=180,
                requires_human_review=True,
            ),
            validation_guidance=ValidationGuidance(
                success_criteria=[
                    "Critical/high vulnerabilities remediated",
                    "No new vulnerabilities introduced",
                    "Security scans pass",
                    "Secrets removed from codebase",
                    "Compliance requirements met",
                ],
                validation_commands=[
                    "# Run full security scan suite",
                    "# Verify secret scanning clean",
                    "# Check dependency vulnerabilities",
                ],
                rollback_guidance="git revert security commits and re-scan",
            ),
            risk=RiskLevel.HIGH,
            evidence_schema=EvidenceSchema(
                required_fields=["scan_results_before", "scan_results_after", "remediation_log", "compliance_check"],
                optional_fields=["threat_model_update", "penetration_test_results"],
                artifact_types=["security_report", "remediation_log", "scan_results"],
            ),
            supported_languages=["python", "typescript", "javascript", "go", "rust", "java", "swift"],
            tags=["security", "vulnerability", "hardening", "compliance"],
        )


# Export all skill classes
__all__ = [
    "BaseSkill",
    "BugfixSkill",
    "RefactorSkill",
    "TestSkill",
    "ReviewSkill",
    "FeatureSkill",
    "DependencySkill",
    "DocumentationSkill",
    "MigrationSkill",
    "PerformanceSkill",
    "SecuritySkill",
]