"""
Progressive Repository Context Builder.

Builds task-aware context for the Coder Brain instead of a flat
file dump:

1. REPO PROFILE: compact technology stack (profiler)
2. RANKED FILES: files ranked by relevance to the goal, included
   within budget; remaining files listed as a compact index
3. LANGUAGE / FRAMEWORK GUIDANCE: conventions for the stack
4. STRATEGY: task classification + verification strategy

Phase 4 of the Elite Coding Intelligence layer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.intelligence.language_adaptation import format_language_guidance
from app.intelligence.repo_profiler import RepoProfiler, format_repo_profile
from app.intelligence.task_strategy import StrategySelection, classify_task
from app.repo_intelligence.engine import RepoIntelligence
from app.repo_intelligence.models import Evidence
from app.repo_intelligence.ranking import target_files
from app.skills.models import SkillId

IGNORED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "coverage",
    ".idea",
    ".vscode",
}

ALLOWED_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".toml", ".yaml",
    ".yml", ".md", ".java", ".kt", ".kts", ".swift", ".go", ".rs",
    ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".sql",
    ".sh", ".html", ".css", ".scss", ".vue", ".svelte", ".dart",
    ".hcl", ".tf", ".proto", ".graphql",
}

MAX_FILE_BYTES = 120_000


@dataclass
class ContextBuild:
    """Result of progressive context construction."""

    profile: Dict[str, Any] = field(default_factory=dict)
    ranked_files: List[dict] = field(default_factory=list)
    strategy: Optional[StrategySelection] = None
    language_guidance: str = ""
    included_files: List[str] = field(default_factory=list)
    excluded_count: int = 0
    sections: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "ranked_files": self.ranked_files,
            "strategy": self.strategy.to_dict() if self.strategy else None,
            "language_guidance": self.language_guidance,
            "included_files": self.included_files,
            "excluded_count": self.excluded_count,
        }


def _list_source_files(worktree: Path) -> List[Path]:
    """List source files (skipping ignored dirs and overlarge files)."""
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(worktree):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def _read_file(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (chars / 4)."""
    return len(text) // 4


class RepoContextBuilder:
    """Builds progressive, budgeted context for a coding goal."""

    def __init__(
        self,
        max_chars: int = 24000,
        max_ranked: int = 12,
        profiler: Optional[RepoProfiler] = None,
    ):
        self.max_chars = max_chars
        self.max_ranked = max_ranked
        self.profiler = profiler or RepoProfiler()
        self._repo_intel = RepoIntelligence()

    def build(self, goal: str, worktree: Path) -> ContextBuild:
        build = ContextBuild()

        # 1. Profile (fast, marker-based + repo intelligence).
        try:
            profile = self.profiler.profile(worktree)
            build.profile = self.profiler.to_dict(profile)
        except Exception:
            build.profile = {"repo_type": "unknown", "primary_language": "unknown"}

        # 2. Strategy selection (goal + profile).
        try:
            build.strategy = classify_task(goal, build.profile)
        except Exception:
            build.strategy = None

        # 3. Language / framework guidance.
        try:
            build.language_guidance = format_language_guidance(build.profile)
        except Exception:
            build.language_guidance = ""

        # 4. Rank files by relevance to the goal.
        files = _list_source_files(worktree)
        rel_paths = [f.relative_to(worktree).as_posix() for f in files]
        contents: Dict[str, str] = {p: _read_file(f) for p, f in zip(rel_paths, files)}
        try:
            evidence = self._repo_intel.analyze(worktree)
            symbols = evidence.symbols
        except Exception:
            symbols = None

        ranked = target_files(goal, rel_paths, symbols=symbols, top_n=60)
        build.ranked_files = [
            {
                "path": r.path,
                "score": round(r.score, 2),
                "reasons": r.reasons,
            }
            for r in ranked[: self.max_ranked]
        ]

        # 5. Fill the budget: profile + strategy + guidance first, then
        # ranked file contents in order, then a compact index of the rest.
        sections: Dict[str, str] = {}

        profile_text = format_repo_profile(build.profile)
        sections["REPOSITORY PROFILE"] = profile_text

        strategy_text = format_strategy(build.strategy)
        if strategy_text:
            sections["TASK ANALYSIS"] = strategy_text

        lang_text = build.language_guidance
        if lang_text:
            sections["LANGUAGE/FRAMEWORK CONVENTIONS"] = lang_text

        used = sum(_estimate_tokens(t) for t in sections.values())
        budget = self.max_chars

        included: List[str] = []
        excluded = 0

        for r in ranked:
            path = r.path
            content = contents.get(path, "")
            if not content:
                continue
            cost = _estimate_tokens(content)
            if used + cost > budget:
                excluded += 1
                continue
            header = f"--- FILE: {path} (relevance {r.score:.2f}) ---"
            sections[header] = content
            used += cost
            included.append(path)
            if used >= budget:
                break

        # Compact index of files that did not fit.
        index_files = [p for p in rel_paths if p not in included]
        index_files.sort()
        index_chunk: List[str] = []
        for p in index_files:
            line = f"  {p}" + (f"  [rank {r.path} {r.score:.1f}]" if False else "")
            index_chunk.append(line)
        index_text = "\n".join(["REMAINING FILES (not shown in full):", *index_chunk])
        sections["FILE INDEX"] = index_text

        build.included_files = included
        build.excluded_count = len(index_files)
        build.sections = sections
        return build

    def render_prompt_block(self, build: ContextBuild) -> str:
        """Render the context as one prompt block."""
        parts = []
        for title, body in build.sections.items():
            if title.startswith("--- FILE:"):
                parts.append(body)
            else:
                parts.append(body)
        return "\n\n".join(parts)


def format_strategy(selection: Optional[StrategySelection]) -> str:
    """Render strategy selection as prompt text."""
    if selection is None:
        return ""
    parts = [
        "TASK STRATEGY (machine-detected):",
        f"  category: {selection.task_type}",
        f"  required capabilities: {', '.join(selection.required_capabilities)}",
    ]
    if selection.optional_capabilities:
        parts.append(
            f"  optional capabilities: {', '.join(selection.optional_capabilities)}"
        )
    if selection.risk_areas:
        parts.append(f"  risk areas: {', '.join(selection.risk_areas)}")
    parts.append(f"  verification strategy: {selection.verification_strategy}")
    parts.append(
        f"  confidence: {selection.confidence:.2f}"
    )
    return "\n".join(parts)


def build_task_context(goal: str, worktree: Path, max_chars: int = 24000) -> dict:
    """One-call API: return a dict ready for prompt assembly."""
    builder = RepoContextBuilder(max_chars=max_chars)
    build = builder.build(goal, worktree)
    return build.to_dict()