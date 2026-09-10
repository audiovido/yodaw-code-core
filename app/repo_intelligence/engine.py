"""RepoIntelligence facade: analyze(path) -> Evidence with caching."""

import os
from pathlib import Path
from typing import Union

from .cache import RepoCache
from .detector import DEFAULT_MAX_FILE_SIZE, DEFAULT_MAX_FILES, build_repo_map
from .graph import build_import_graph
from .models import Evidence, RepoType
from .symbols import index_symbols
from .tests_discovery import discover_tests, is_test_file, map_source_to_test


class RepoIntelligence:
    """High-level facade over detection, map, symbols, graph, and tests."""

    def __init__(
        self,
        max_files: int = DEFAULT_MAX_FILES,
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        cache: Union[RepoCache, None] = None,
    ) -> None:
        self.max_files = max_files
        self.max_file_size = max_file_size
        self.cache = cache or RepoCache()

    def analyze(self, path: Union[str, os.PathLike]) -> Evidence:
        """Analyze a repo directory into JSON-serializable Evidence."""
        base = Path(path)
        repo_map = build_repo_map(base, self.max_files, self.max_file_size)
        rels = [f.path for f in repo_map.files]
        cached = self.cache.get(base, "evidence", rels)
        if isinstance(cached, Evidence):
            return cached
        symbols = index_symbols(base, rels, self.max_file_size)
        edges = build_import_graph(base, rels, self.max_file_size)
        tests = discover_tests(rels)
        source_to_test = map_source_to_test(rels)
        for node in repo_map.files:
            node.is_test = is_test_file(node.path)
        evidence = Evidence(
            root=str(base),
            repo_type=repo_map.repo_type,
            file_count=len(repo_map.files),
            total_size=repo_map.total_size,
            language_breakdown=dict(repo_map.language_breakdown),
            files=list(repo_map.files),
            symbols=symbols,
            edges=edges,
            tests=tests,
            source_to_test=source_to_test,
            skipped_files=repo_map.skipped_files,
            truncated=repo_map.truncated,
        )
        self.cache.put(base, "evidence", rels, evidence)
        return evidence

    def build_evidence(self, path: Union[str, os.PathLike]) -> dict:
        """Return analyze(path) as a JSON-serializable dict."""
        return self.analyze(path).to_dict()

    def invalidate(self, path: Union[str, os.PathLike, None] = None) -> int:
        """Invalidate cached analyses (optionally scoped to a path)."""
        return self.cache.invalidate(path)

    @staticmethod
    def detect(path: Union[str, os.PathLike]) -> RepoType:
        """Detect the repo type for a directory."""
        from .detector import detect_repo_type
        return detect_repo_type(path)
