"""Test file discovery and source<->test mapping per language."""

from pathlib import PurePosixPath

# Naming conventions per language family.
PY_TEST_PREFIXES = ("test_",)
PY_TEST_SUFFIXES = ("_test.py",)
JS_TEST_SUFFIXES = (".test.js", ".test.jsx", ".test.ts", ".test.tsx", ".spec.js", ".spec.ts")
GO_TEST_SUFFIX = "_test.go"
RUST_TEST_DIRS = ("tests/",)

TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs"})


def is_test_file(path: str) -> bool:
    """Return True if the path matches a test naming convention."""
    name = path.rsplit("/", 1)[-1]
    lower = path.lower()
    if name.startswith(PY_TEST_PREFIXES) and name.endswith(".py"):
        return True
    if name.endswith(PY_TEST_SUFFIXES):
        return True
    if lower.endswith(JS_TEST_SUFFIXES):
        return True
    if name.endswith(GO_TEST_SUFFIX):
        return True
    if "/tests/" in lower or lower.startswith("tests/"):
        if lower.endswith(".rs"):
            return True
    if lower.endswith("_test.rs"):
        return True
    parts = set(lower.split("/")[:-1])
    if parts & TEST_DIR_NAMES and (lower.endswith(".py") or lower.endswith(".js")
                                   or lower.endswith(".ts") or lower.endswith(".go")):
        return True
    return False


def _strip_test_markers(stem: str) -> str:
    """Remove test prefixes/suffixes from a file stem."""
    low = stem.lower()
    if low.startswith("test_"):
        stem = stem[5:]
        low = stem.lower()
    for suffix in ("_test", ".test", ".spec"):
        if low.endswith(suffix):
            stem = stem[: -len(suffix)]
            low = stem.lower()
    return stem


def discover_tests(files: list[str]) -> list[str]:
    """Return sorted test files from a file list."""
    return sorted(p for p in files if is_test_file(p))


def map_source_to_test(files: list[str]) -> dict[str, str | None]:
    """Map each non-test source file to its likely test file (or None)."""
    tests = discover_tests(files)
    by_stem: dict[str, list[str]] = {}
    for t in tests:
        stem = PurePosixPath(t).stem
        key = _strip_test_markers(stem).lower()
        by_stem.setdefault(key, []).append(t)
    for key in by_stem:
        by_stem[key].sort()
    result: dict[str, str | None] = {}
    for path in sorted(files):
        if is_test_file(path):
            continue
        suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        if suffix not in (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs"):
            continue
        stem = PurePosixPath(path).stem.lower()
        cands = by_stem.get(stem, [])
        # Prefer a test in a sibling tests/ dir or same dir.
        src_dir = path.rsplit("/", 1)[0] if "/" in path else ""
        best: str | None = cands[0] if cands else None
        for cand in cands:
            cand_dir = cand.rsplit("/", 1)[0] if "/" in cand else ""
            if cand_dir == src_dir or cand_dir.rstrip("s") == src_dir or "test" in cand_dir:
                best = cand
                break
        result[path] = best
    return result
