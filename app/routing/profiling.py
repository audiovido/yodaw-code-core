"""Task profiling for adaptive routing.

Produces a structured TaskProfile from task metadata. Pure
function: no network, no model calls, no side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES = (
    "bugfix",
    "refactor",
    "feature",
    "test",
    "review",
    "dependency",
    "migration",
    "performance",
    "security",
    "docs",
    "mixed",
    "general",
)

COMPLEXITIES = ("trivial", "simple", "medium", "complex", "hard")

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bugfix": ("bug", "fix", "error", "crash", "regression", "broken"),
    "refactor": ("refactor", "rename", "cleanup", "extract", "restructure"),
    "feature": ("feature", "implement", "add", "support", "endpoint"),
    "test": ("test", "coverage", "pytest", "unittest", "spec"),
    "review": ("review", "audit", "inspect"),
    "dependency": ("depend", "upgrade", "version", "package", "lockfile"),
    "migration": ("migrat", "port", "upgrade-framework"),
    "performance": ("performance", "optimiz", "latency", "slow"),
    "security": ("security", "vulnerab", "cve", "injection", "auth"),
    "docs": ("doc", "readme", "changelog"),
}

_COMPLEXITY_SIGNALS: tuple[tuple[str, float], ...] = (
    ("multi-file", 0.5),
    ("multiple", 0.5),
    ("across", 0.3),
    ("entire", 0.6),
    ("refactor", 0.4),
    ("migrat", 0.6),
    ("distributed", 0.7),
    ("concurrent", 0.5),
    ("ambiguous", 0.5),
)

_LANGUAGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "python": (".py", "python", "pytest", "pip"),
    "javascript": (".js", ".jsx", "node", "npm"),
    "typescript": (".ts", ".tsx", "tsc", "deno"),
    "go": (".go", "golang", "go.mod"),
    "rust": (".rs", "cargo", "rustc"),
    "java": (".java", "maven", "gradle"),
}


@dataclass
class TaskProfile:
    """Structured description of one routing request."""

    category: str
    complexity: str
    estimated_context: int
    reasoning_requirement: float
    coding_requirement: float
    tool_requirement: float
    latency_sensitivity: float
    cost_sensitivity: float
    language: str = "unknown"
    framework: str = "unknown"
    retry_count: int = 0

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "complexity": self.complexity,
            "estimated_context": self.estimated_context,
            "reasoning_requirement": self.reasoning_requirement,
            "coding_requirement": self.coding_requirement,
            "tool_requirement": self.tool_requirement,
            "latency_sensitivity": self.latency_sensitivity,
            "cost_sensitivity": self.cost_sensitivity,
            "language": self.language,
            "framework": self.framework,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskProfile":
        known = {
            "category",
            "complexity",
            "estimated_context",
            "reasoning_requirement",
            "coding_requirement",
            "tool_requirement",
            "latency_sensitivity",
            "cost_sensitivity",
            "language",
            "framework",
            "retry_count",
        }
        clean = {key: value for key, value in data.items() if key in known}
        clean.setdefault("category", "general")
        clean.setdefault("complexity", "medium")
        clean.setdefault("estimated_context", 8000)
        for field in (
            "reasoning_requirement",
            "coding_requirement",
            "tool_requirement",
            "latency_sensitivity",
            "cost_sensitivity",
        ):
            clean.setdefault(field, 0.5)
        clean.setdefault("language", "unknown")
        clean.setdefault("framework", "unknown")
        clean.setdefault("retry_count", 0)
        return cls(**clean)


def _tokens(text: str) -> set[str]:
    return {match.lower() for match in _TOKEN_RE.findall(text or "")}


def _detect_category(text: str, hint: str | None) -> str:
    if hint and hint.strip().lower() in CATEGORIES:
        return hint.strip().lower()
    lowered = (text or "").lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return "general"


def _detect_complexity(text: str, hint: str | None, files: int) -> str:
    if hint and hint.strip().lower() in COMPLEXITIES:
        return hint.strip().lower()
    lowered = (text or "").lower()
    weight = 0.0
    for signal, value in _COMPLEXITY_SIGNALS:
        if signal in lowered:
            weight += value
    length = len(text or "")
    if length > 2000:
        weight += 0.6
    elif length > 800:
        weight += 0.3
    if files >= 5:
        weight += 0.7
    elif files >= 2:
        weight += 0.3
    if weight >= 1.5:
        return "hard"
    if weight >= 1.0:
        return "complex"
    if weight >= 0.5:
        return "medium"
    if weight > 0.0:
        return "simple"
    return "trivial" if length < 120 and files <= 1 else "simple"


def _estimate_context(text: str, files: int, history_tokens: int) -> int:
    base = 4000
    estimate = base + len(text or "") * 2 + files * 2500 + history_tokens
    return max(1000, min(200000, estimate))


def _detect_language(text: str, hint: str | None) -> str:
    if hint and hint.strip():
        return hint.strip().lower()
    lowered = (text or "").lower()
    tokens = _tokens(text or "")
    for language, markers in _LANGUAGE_PATTERNS.items():
        if any(marker in lowered for marker in markers):
            return language
        if language in tokens:
            return language
    return "unknown"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def build_task_profile(
    task: str,
    category_hint: str | None = None,
    complexity_hint: str | None = None,
    language_hint: str | None = None,
    framework: str | None = None,
    files_changed: int = 1,
    history_tokens: int = 0,
    latency_sensitivity: float = 0.5,
    cost_sensitivity: float = 0.5,
    tools_required: bool = False,
    retry_count: int = 0,
) -> TaskProfile:
    """Build a structured profile from free-form task metadata."""
    category = _detect_category(task, category_hint)
    complexity = _detect_complexity(task, complexity_hint, files_changed)
    weights = {
        "trivial": (0.1, 0.2, 0.0),
        "simple": (0.3, 0.5, 0.2),
        "medium": (0.5, 0.6, 0.4),
        "complex": (0.7, 0.7, 0.6),
        "hard": (0.9, 0.8, 0.8),
    }
    reasoning, coding, tools = weights[complexity]
    if tools_required:
        tools = max(tools, 0.8)
    if category in ("review", "security"):
        reasoning = max(reasoning, 0.7)
    if category in ("feature", "bugfix", "migration"):
        coding = max(coding, 0.6)
    if retry_count > 0:
        reasoning = min(1.0, reasoning + 0.1 * retry_count)
    return TaskProfile(
        category=category,
        complexity=complexity,
        estimated_context=_estimate_context(task, files_changed, history_tokens),
        reasoning_requirement=_clamp(reasoning),
        coding_requirement=_clamp(coding),
        tool_requirement=_clamp(tools),
        latency_sensitivity=_clamp(latency_sensitivity),
        cost_sensitivity=_clamp(cost_sensitivity),
        language=_detect_language(task, language_hint),
        framework=(framework or "unknown").strip().lower() or "unknown",
        retry_count=max(0, int(retry_count or 0)),
    )
