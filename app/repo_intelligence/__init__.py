"""Repo intelligence & context engine (repo-type detection, maps, symbols, graphs)."""

from .budget import Budget, BudgetEntry, BudgetResult, estimate_file_tokens, estimate_tokens, fit_budget
from .cache import RepoCache
from .detector import (
    DEFAULT_MAX_FILE_SIZE,
    DEFAULT_MAX_FILES,
    build_repo_map,
    detect_repo_type,
    find_marker,
    list_repo_files,
)
from .engine import RepoIntelligence
from .graph import build_import_graph, forward_dependencies, reverse_dependencies
from .impact import blast_radius
from .models import (
    Budget as BudgetModel,
    Evidence,
    FileNode,
    ImportEdge,
    RankedFile,
    RepoMap,
    RepoType,
    Symbol,
)
from .ranking import rank_files, target_files
from .symbols import discover_symbols_for_file, index_symbols
from .tests_discovery import discover_tests, is_test_file, map_source_to_test

__all__ = [
    "Budget",
    "BudgetEntry",
    "BudgetModel",
    "BudgetResult",
    "DEFAULT_MAX_FILE_SIZE",
    "DEFAULT_MAX_FILES",
    "Evidence",
    "FileNode",
    "ImportEdge",
    "RankedFile",
    "RepoCache",
    "RepoIntelligence",
    "RepoMap",
    "RepoType",
    "Symbol",
    "blast_radius",
    "build_import_graph",
    "build_repo_map",
    "detect_repo_type",
    "discover_symbols_for_file",
    "discover_tests",
    "estimate_file_tokens",
    "estimate_tokens",
    "find_marker",
    "fit_budget",
    "forward_dependencies",
    "index_symbols",
    "is_test_file",
    "list_repo_files",
    "map_source_to_test",
    "rank_files",
    "reverse_dependencies",
    "target_files",
]


def build_evidence(path) -> dict:
    """Analyze a repo path into a JSON-serializable evidence dict."""
    return RepoIntelligence().build_evidence(path)
