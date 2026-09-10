"""Repo type detection and repository map building."""

import os
from pathlib import Path
from typing import Optional, Union

from .models import FileNode, RepoMap, RepoType, SKIP_DIRS, language_for_path

# Marker file -> repo type, checked in priority order.
MARKERS: tuple[tuple[RepoType, tuple[str, ...]], ...] = (
    (RepoType.PYTHON, ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")),
    (RepoType.NODE, ("package.json",)),
    (RepoType.GO, ("go.mod",)),
    (RepoType.RUST, ("Cargo.toml",)),
)

DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_FILE_SIZE = 1_000_000


def detect_repo_type(root: Union[str, os.PathLike]) -> RepoType:
    """Detect repo type from marker files (highest-priority match wins)."""
    base = Path(root)
    present = set()
    try:
        for child in base.iterdir():
            if child.is_file():
                present.add(child.name)
    except OSError:
        return RepoType.GENERIC
    for repo_type, markers in MARKERS:
        if any(m in present for m in markers):
            return repo_type
    return RepoType.GENERIC


def is_binary_file(path: Path, chunk_size: int = 4096) -> bool:
    """Check for NUL bytes in the leading chunk (binary heuristic)."""
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(chunk_size)
    except OSError:
        return True


def _count_files_below(directory: Path) -> int:
    """Count files under a directory (used for excluded-dir skip stats)."""
    count = 0
    for _, _, filenames in os.walk(directory):
        count += len(filenames)
    return count


def _walk_files(root: Path) -> tuple[list[Path], int]:
    """Return (sorted relative file paths, excluded-dir file count)."""
    collected: list[Path] = []
    excluded = 0
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_dir:
                if entry.name in SKIP_DIRS:
                    try:
                        excluded += _count_files_below(entry)
                    except OSError:
                        pass
                    continue
                stack.append(entry)
            elif entry.is_file():
                collected.append(entry.relative_to(root))
    collected.sort()
    return collected, excluded


def list_repo_files(
    root: Union[str, os.PathLike],
    max_files: int = DEFAULT_MAX_FILES,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> tuple[list[FileNode], int, bool]:
    """List scannable files; returns (nodes, skipped, truncated)."""
    base = Path(root)
    rel_paths, excluded = _walk_files(base)
    nodes: list[FileNode] = []
    skipped = excluded
    truncated = False
    for rel in rel_paths:
        if len(nodes) >= max_files:
            truncated = True
            skipped += len(rel_paths) - rel_paths.index(rel)
            break
        full = base / rel
        try:
            size = full.stat().st_size
        except OSError:
            skipped += 1
            continue
        if size > max_file_size:
            skipped += 1
            continue
        if is_binary_file(full):
            skipped += 1
            continue
        posix = rel.as_posix()
        nodes.append(FileNode(path=posix, size=size, language=language_for_path(posix)))
    nodes.sort(key=lambda n: n.path)
    return nodes, skipped, truncated


def build_repo_map(
    root: Union[str, os.PathLike],
    max_files: int = DEFAULT_MAX_FILES,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> RepoMap:
    """Build the directory tree snapshot with sizes and languages."""
    base = Path(root)
    repo_type = detect_repo_type(base)
    files, skipped, truncated = list_repo_files(base, max_files, max_file_size)
    breakdown: dict[str, int] = {}
    total = 0
    for node in files:
        lang = node.language or "unknown"
        breakdown[lang] = breakdown.get(lang, 0) + 1
        total += node.size
    return RepoMap(
        root=str(base),
        repo_type=repo_type,
        files=files,
        language_breakdown=dict(sorted(breakdown.items())),
        total_size=total,
        skipped_files=skipped,
        truncated=truncated,
    )


def find_marker(root: Union[str, os.PathLike]) -> Optional[str]:
    """Return the first matching marker filename, or None."""
    base = Path(root)
    for _, markers in MARKERS:
        for marker in markers:
            if (base / marker).is_file():
                return marker
    return None
