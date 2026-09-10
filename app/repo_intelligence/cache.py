"""mtime+size keyed cache for repo analysis."""

import os
from pathlib import Path
from typing import Any, Hashable, Union


class RepoCache:
    """Simple mtime+size keyed cache with explicit invalidation."""

    def __init__(self) -> None:
        self._store: dict[Hashable, tuple[tuple[float, int], Any]] = {}

    def _key(self, root: Union[str, os.PathLike], name: str) -> tuple[str, str]:
        """Build a cache key from root and entry name."""
        return (str(Path(root)), name)

    def fingerprint(self, root: Union[str, os.PathLike], paths: list[str]) -> tuple[float, int]:
        """Combine max mtime and total size over the given relative paths."""
        base = Path(root)
        max_mtime = 0.0
        total_size = 0
        for rel in sorted(paths):
            try:
                stat = (base / rel).stat()
                max_mtime = max(max_mtime, stat.st_mtime)
                total_size += stat.st_size
            except OSError:
                max_mtime = max(max_mtime, -1.0)
        return (max_mtime, total_size)

    def get(self, root: Union[str, os.PathLike], name: str, paths: list[str]) -> Union[Any, None]:
        """Return cached value if the fingerprint still matches, else None."""
        key = self._key(root, name)
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry[0] != self.fingerprint(root, paths):
            return None
        return entry[1]

    def put(self, root: Union[str, os.PathLike], name: str, paths: list[str], value: Any) -> None:
        """Store a value with the current fingerprint."""
        self._store[self._key(root, name)] = (self.fingerprint(root, paths), value)

    def invalidate(self, root: Union[str, os.PathLike, None] = None, name: Union[str, None] = None) -> int:
        """Drop entries; returns the number removed."""
        if root is None and name is None:
            count = len(self._store)
            self._store.clear()
            return count
        doomed = [
            k for k in self._store
            if (root is None or k[0] == str(Path(root))) and (name is None or k[1] == name)
        ]
        for k in doomed:
            del self._store[k]
        return len(doomed)

    def __len__(self) -> int:
        return len(self._store)
