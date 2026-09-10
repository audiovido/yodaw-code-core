"""
Task classifier for YODAW Coder Skills.

Classifies incoming tasks into primary skill, secondary skills, and provides
confidence scoring with reasoning.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import (
    Intent,
    ClassificationResult,
    SkillId,
    Skill,
    RiskLevel,
)


@dataclass
class ClassificationRule:
    """A rule for classifying tasks."""
    pattern: str
    intent: Intent
    primary_skill: SkillId
    secondary_skills: list[SkillId] = field(default_factory=list)
    confidence: float = 0.8
    keywords: list[str] = field(default_factory=list)
    language_hints: list[str] = field(default_factory=list)
    compiled_pattern: Any = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.compiled_pattern = re.compile(self.pattern, re.IGNORECASE)


class TaskClassifier:
    """Classifies tasks into skills based on content analysis."""

    DEFAULT_RULES: list[ClassificationRule] = [
        # Bugfix rules
        ClassificationRule(
            pattern=r"(fix|bug|error|crash|exception|issue|defect|broken|failing)",
            intent=Intent.BUGFIX,
            primary_skill="bugfix",
            secondary_skills=["test", "review"],
            confidence=0.85,
            keywords=["fix", "bug", "error", "crash", "exception", "issue", "defect", "broken", "failing", "regression"],
        ),
        ClassificationRule(
            pattern=r"(reproduce|minimal.*repro|steps.*to.*reproduce)",
            intent=Intent.BUGFIX,
            primary_skill="bugfix",
            secondary_skills=["test"],
            confidence=0.9,
            keywords=["reproduce", "minimal repro", "steps to reproduce"],
        ),

        # Refactor rules
        ClassificationRule(
            pattern=r"(refactor|cleanup|restructure|reorganize|simplify|modernize|extract|inline|rename|move)",
            intent=Intent.REFACTOR,
            primary_skill="refactor",
            secondary_skills=["test", "review"],
            confidence=0.85,
            keywords=["refactor", "cleanup", "restructure", "reorganize", "simplify", "modernize", "extract", "inline", "rename", "move"],
        ),
        ClassificationRule(
            pattern=r"(technical debt|code smell|duplication|complexity|coupling|cohesion)",
            intent=Intent.REFACTOR,
            primary_skill="refactor",
            secondary_skills=["review"],
            confidence=0.8,
            keywords=["technical debt", "code smell", "duplication", "complexity", "coupling", "cohesion"],
        ),

        # Test rules
        ClassificationRule(
            pattern=r"(test|testing|unit test|integration test|e2e|coverage|mock|fixture|assert)",
            intent=Intent.TEST,
            primary_skill="test",
            secondary_skills=["bugfix", "refactor"],
            confidence=0.85,
            keywords=["test", "testing", "unit test", "integration test", "e2e", "coverage", "mock", "fixture", "assert"],
        ),
        ClassificationRule(
            pattern=r"(add test|write test|missing test|test coverage)",
            intent=Intent.TEST,
            primary_skill="test",
            secondary_skills=[],
            confidence=0.9,
            keywords=["add test", "write test", "missing test", "test coverage"],
        ),

        # Review rules
        ClassificationRule(
            pattern=r"(review|audit|inspect|analyze|check|verify|validate|security review|code review)",
            intent=Intent.REVIEW,
            primary_skill="review",
            secondary_skills=["security", "refactor"],
            confidence=0.85,
            keywords=["review", "audit", "inspect", "analyze", "check", "verify", "validate", "security review", "code review"],
        ),
        ClassificationRule(
            pattern=r"(read.?only|read only|assessment|evaluation)",
            intent=Intent.REVIEW,
            primary_skill="review",
            secondary_skills=[],
            confidence=0.8,
            keywords=["read-only", "read only", "assessment", "evaluation"],
        ),

        # Feature rules
        ClassificationRule(
            pattern=r"(implement|add|create|build|develop|new feature|feature|functionality|capability)",
            intent=Intent.FEATURE,
            primary_skill="feature",
            secondary_skills=["test", "documentation", "review"],
            confidence=0.8,
            keywords=["implement", "add", "create", "build", "develop", "new feature", "feature", "functionality", "capability"],
        ),
        ClassificationRule(
            pattern=r"(endpoint|api|route|handler|service|component|module|class|function)",
            intent=Intent.FEATURE,
            primary_skill="feature",
            secondary_skills=["test", "documentation"],
            confidence=0.75,
            keywords=["endpoint", "api", "route", "handler", "service", "component", "module", "class", "function"],
        ),

        # Dependency rules
        ClassificationRule(
            pattern=r"(dependenc|library|package|import|require|install|upgrade|downgrade|vulnerab|outdated|deprecat)",
            intent=Intent.DEPENDENCY,
            primary_skill="dependency",
            secondary_skills=["test", "security"],
            confidence=0.85,
            keywords=["dependency", "library", "package", "import", "require", "install", "upgrade", "downgrade", "vulnerability", "outdated", "deprecate"],
        ),
        ClassificationRule(
            pattern=r"(reuse|existing|internal|shared|common|utility|helper|framework|sdk)",
            intent=Intent.DEPENDENCY,
            primary_skill="dependency",
            secondary_skills=["feature", "refactor"],
            confidence=0.75,
            keywords=["reuse", "existing", "internal", "shared", "common", "utility", "helper", "framework", "sdk"],
        ),

        # Documentation rules
        ClassificationRule(
            pattern=r"(document|docstring|readme|changelog|api doc|comment|guide|tutorial|spec|specification)",
            intent=Intent.DOCUMENTATION,
            primary_skill="documentation",
            secondary_skills=["review"],
            confidence=0.85,
            keywords=["document", "docstring", "readme", "changelog", "api doc", "comment", "guide", "tutorial", "spec", "specification"],
        ),

        # Migration rules
        ClassificationRule(
            pattern=r"(migrat|upgrade|port|convert|transpile|modernize|legacy|deprecat|sunset)",
            intent=Intent.MIGRATION,
            primary_skill="migration",
            secondary_skills=["test", "refactor", "review"],
            confidence=0.8,
            keywords=["migrate", "upgrade", "port", "convert", "transpile", "modernize", "legacy", "deprecate", "sunset"],
        ),

        # Performance rules
        ClassificationRule(
            pattern=r"(performance|optimiz|speed|latency|throughput|memory|cpu|benchmark|profil|bottleneck|slow)",
            intent=Intent.PERFORMANCE,
            primary_skill="performance",
            secondary_skills=["test", "review", "refactor"],
            confidence=0.85,
            keywords=["performance", "optimize", "speed", "latency", "throughput", "memory", "cpu", "benchmark", "profile", "bottleneck", "slow"],
        ),

        # Security rules
        ClassificationRule(
            pattern=r"(secur|vulnerab|exploit|inject|xss|csrf|auth|authoriz|encrypt|decrypt|secret|token|credential|audit)",
            intent=Intent.SECURITY,
            primary_skill="security",
            secondary_skills=["review", "test", "refactor"],
            confidence=0.9,
            keywords=["security", "vulnerability", "exploit", "inject", "xss", "csrf", "auth", "authoriz", "encrypt", "decrypt", "secret", "token", "credential", "audit"],
        ),
    ]

    def __init__(self, skills: dict[SkillId, Skill], custom_rules: Optional[list[ClassificationRule]] = None):
        self.skills = skills
        self.rules = custom_rules or self.DEFAULT_RULES

    def classify(self, task_description: str, context: Optional[dict[str, Any]] = None) -> ClassificationResult:
        """
        Classify a task description into skills.

        Args:
            task_description: The task description to classify
            context: Optional context (e.g., project language, file paths, etc.)

        Returns:
            ClassificationResult with primary skill, secondary skills, confidence, and reason
        """
        context = context or {}
        task_lower = task_description.lower()

        # Score each rule
        rule_scores: list[tuple[ClassificationRule, float]] = []
        for rule in self.rules:
            score = self._score_rule(rule, task_lower, context)
            if score > 0:
                rule_scores.append((rule, score))

        if not rule_scores:
            # Default to feature implementation
            return ClassificationResult(
                primary_skill="feature",
                secondary_skills=["test", "documentation"],
                confidence=0.5,
                reason="No specific pattern matched; defaulting to feature implementation",
                detected_intent=Intent.FEATURE,
                detected_language=context.get("language"),
            )

        # Sort by score
        rule_scores.sort(key=lambda x: x[1], reverse=True)

        best_rule, best_score = rule_scores[0]

        # Collect secondary skills from top rules
        secondary_skills = []
        for rule, score in rule_scores[1:4]:  # Top 3 alternatives
            if rule.primary_skill != best_rule.primary_skill:
                secondary_skills.append(rule.primary_skill)
            secondary_skills.extend(rule.secondary_skills)

        # Deduplicate and remove primary
        secondary_skills = list(dict.fromkeys(secondary_skills))
        if best_rule.primary_skill in secondary_skills:
            secondary_skills.remove(best_rule.primary_skill)

        # Detect language from context or task description
        detected_language = self._detect_language(task_description, context)

        return ClassificationResult(
            primary_skill=best_rule.primary_skill,
            secondary_skills=secondary_skills[:5],  # Limit to 5
            confidence=min(best_score, 1.0),
            reason=self._generate_reason(best_rule, task_description, context),
            detected_intent=best_rule.intent,
            detected_language=detected_language,
            metadata={
                "matched_keywords": self._get_matched_keywords(best_rule, task_lower),
                "alternative_rules": [(r.intent.value, s) for r, s in rule_scores[1:4]],
            },
        )

    def _score_rule(self, rule: ClassificationRule, task_lower: str, context: dict[str, Any]) -> float:
        """Score a rule against the task description."""
        score = 0.0

        # Pattern match - higher weight
        if rule.compiled_pattern.search(task_lower):
            score += rule.confidence * 0.75

        # Keyword matches - more generous scoring
        keyword_matches = sum(1 for kw in rule.keywords if kw.lower() in task_lower)
        if keyword_matches > 0:
            score += min(keyword_matches * 0.15, 0.4)

        # Language hint bonus
        if context.get("language"):
            lang = context["language"].lower()
            if lang in [h.lower() for h in rule.language_hints]:
                score += 0.15

        # Context bonuses
        if context.get("file_paths"):
            for path in context["file_paths"]:
                path_lower = path.lower()
                if any(kw in path_lower for kw in rule.keywords):
                    score += 0.1
                    break

        # Boost for strong intent indicators at the beginning
        for kw in rule.keywords:
            if task_lower.startswith(kw.lower()):
                score += 0.1
                break

        return min(score, 1.0)

    def _detect_language(self, task_description: str, context: dict[str, Any]) -> Optional[str]:
        """Detect programming language from context or task description."""
        if context.get("language"):
            return context["language"]

        task_lower = task_description.lower()
        language_indicators = {
            "python": ["python", "py", "pytest", "pip", "poetry", "fastapi", "django", "flask", "async def", "def ", "import "],
            "typescript": ["typescript", "ts", "npm", "yarn", "deno", "interface ", "type ", "const ", "export "],
            "javascript": ["javascript", "js", "node", "npm", "yarn", "require(", "module.exports", "async function"],
            "go": ["golang", "go ", "go.mod", "func ", "package ", "import (", "go test"],
            "rust": ["rust", "cargo", "fn ", "struct ", "impl ", "use ", "mod ", "cargo.toml"],
            "java": ["java", "maven", "gradle", "public class", "private void", "import java", "spring boot"],
            "swift": ["swift", "xcode", "func ", "var ", "let ", "import Foundation", "swift package"],
        }

        for lang, indicators in language_indicators.items():
            if any(ind in task_lower for ind in indicators):
                return lang

        return None

    def _get_matched_keywords(self, rule: ClassificationRule, task_lower: str) -> list[str]:
        """Get keywords that matched in the task description."""
        return [kw for kw in rule.keywords if kw.lower() in task_lower]

    def _generate_reason(self, rule: ClassificationRule, task_description: str, context: dict[str, Any]) -> str:
        """Generate a human-readable reason for the classification."""
        matched = self._get_matched_keywords(rule, task_description.lower())
        lang = context.get("language", "unknown")

        parts = [
            f"Matched {rule.intent.value} intent",
            f"Keywords: {', '.join(matched[:5])}" if matched else "Pattern match",
            f"Language context: {lang}",
        ]

        return "; ".join(parts)

    def add_rule(self, rule: ClassificationRule):
        """Add a custom classification rule."""
        # Pattern is already compiled in __post_init__
        self.rules.append(rule)

    def remove_rule(self, pattern: str):
        """Remove a rule by pattern."""
        self.rules = [r for r in self.rules if r.pattern != pattern]