"""Models for the repo intelligence & context engine."""

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class RepoType(str, Enum):
    """Detected repository type."""
    PYTHON = "python"
    NODE = "node"
    GO = "go"
    RUST = "rust"
    GENERIC = "generic"


# Directories never descended into when scanning a repository.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "__pycache__", ".venv", "venv", ".tox",
    "node_modules", "target", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

# File extension -> language label.
EXTENSION_LANGUAGE = {
    ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".sh": "shell",
    ".md": "markdown",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml",
}

UNKNOWN_LANGUAGE = "unknown"


def language_for_path(path: str) -> str:
    """Return the language label for a file path, or '' if unknown."""
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return EXTENSION_LANGUAGE.get(name[dot:].lower(), "")


@dataclass
class FileNode:
    """A single file in the repository map."""
    path: str  # repo-relative posix path
    size: int
    language: str = ""
    is_test: bool = False
    is_binary: bool = False


@dataclass
class Symbol:
    """A discovered code symbol (definition or import)."""
    name: str
    kind: str  # "function" | "class" | "import"
    file: str  # repo-relative posix path
    line: int


@dataclass
class ImportEdge:
    """A file-level dependency edge (src imports dst)."""
    src: str
    dst: str  # resolved repo-relative path, or raw spec when unresolvable
    raw: str


@dataclass
class RankedFile:
    """A file ranked for relevance to a task."""
    path: str
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Budget:
    """Token budget configuration."""
    max_total_tokens: int = 12000
    max_file_tokens: int = 2000


@dataclass
class BudgetEntry:
    """Per-file budgeting outcome."""
    path: str
    tokens: int
    truncated: bool = False
    included: bool = True


@dataclass
class BudgetResult:
    """Outcome of fitting files into a token budget."""
    entries: list[BudgetEntry]
    total_tokens: int
    within_budget: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict."""
        return {
            "entries": [asdict(e) for e in self.entries],
            "total_tokens": self.total_tokens,
            "within_budget": self.within_budget,
        }


@dataclass
class RepoMap:
    """Directory tree snapshot with sizes and language breakdown."""
    root: str
    repo_type: RepoType
    files: list[FileNode]
    language_breakdown: dict[str, int]
    total_size: int
    skipped_files: int = 0
    truncated: bool = False


@dataclass
class Evidence:
    """Full repo intelligence analysis result."""
    root: str
    repo_type: RepoType
    file_count: int
    total_size: int
    language_breakdown: dict[str, int]
    files: list[FileNode]
    symbols: dict[str, list[Symbol]]
    edges: list[ImportEdge]
    tests: list[str]
    source_to_test: dict[str, Optional[str]]
    skipped_files: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict of all analyses."""
        return {
            "root": self.root,
            "repo_type": self.repo_type.value,
            "file_count": self.file_count,
            "total_size": self.total_size,
            "language_breakdown": dict(self.language_breakdown),
            "files": [asdict(f) for f in self.files],
            "symbols": {k: [asdict(s) for s in v] for k, v in self.symbols.items()},
            "edges": [asdict(e) for e in self.edges],
            "tests": list(self.tests),
            "source_to_test": dict(self.source_to_test),
            "skipped_files": self.skipped_files,
            "truncated": self.truncated,
        }
