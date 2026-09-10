"""Regex-based symbol discovery for common languages."""

import re
from pathlib import Path
from typing import Union

from .models import Symbol

_PY_DEF = re.compile(r"^\s*def\s+([A-Za-z_]\w*)", re.M)
_PY_CLASS = re.compile(r"^\s*class\s+([A-Za-z_]\w*)", re.M)
_PY_IMPORT = re.compile(r"^\s*import\s+(.+)$", re.M)
_PY_FROM = re.compile(r"^\s*from\s+(\.+[\w\.]*|\S+)\s+import\s+(.+)$", re.M)

_JS_FUNC = re.compile(r"^\s*(?:export\s+)?function\s+([A-Za-z_]\w*)", re.M)
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_]\w*)", re.M)
_JS_CONST_FN = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=\s*(?:async\s*)?(?:\([^)]*\)\s*=>|function)",
    re.M,
)
_JS_IMPORT = re.compile(r"^\s*import\s+(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]", re.M)
_JS_REQUIRE = re.compile(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", re.M)

_GO_FUNC = re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.M)
_GO_TYPE = re.compile(r"^\s*type\s+([A-Za-z_]\w*)", re.M)
_GO_IMPORT = re.compile(r'^\s*(?:[A-Za-z_\.]\w*\s+)?"([^"]+)"', re.M)

_RS_FN = re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)", re.M)
_RS_STRUCT = re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait|mod)\s+([A-Za-z_]\w*)", re.M)
_RS_USE = re.compile(r"^\s*use\s+([^;]+);", re.M)

SUPPORTED_EXTENSIONS = frozenset({".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs"})


def _line_of(text: str, pos: int) -> int:
    """Return 1-based line number for an offset."""
    return text.count("\n", 0, pos) + 1


def discover_symbols_for_file(rel_path: str, text: str) -> list[Symbol]:
    """Discover symbols in file text; graceful fallback ([]) for others."""
    suffix = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path.rsplit("/", 1)[-1] else ""
    out: list[Symbol] = []
    if suffix == ".py":
        for m in _PY_DEF.finditer(text):
            out.append(Symbol(name=m.group(1), kind="function", file=rel_path, line=_line_of(text, m.start())))
        for m in _PY_CLASS.finditer(text):
            out.append(Symbol(name=m.group(1), kind="class", file=rel_path, line=_line_of(text, m.start())))
        for m in _PY_IMPORT.finditer(text):
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip().split(".")[0]
                if name:
                    out.append(Symbol(name=name, kind="import", file=rel_path, line=_line_of(text, m.start())))
        for m in _PY_FROM.finditer(text):
            out.append(Symbol(name=m.group(1), kind="import", file=rel_path, line=_line_of(text, m.start())))
    elif suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
        for m in _JS_FUNC.finditer(text):
            out.append(Symbol(name=m.group(1), kind="function", file=rel_path, line=_line_of(text, m.start())))
        for m in _JS_CLASS.finditer(text):
            out.append(Symbol(name=m.group(1), kind="class", file=rel_path, line=_line_of(text, m.start())))
        for m in _JS_CONST_FN.finditer(text):
            out.append(Symbol(name=m.group(1), kind="function", file=rel_path, line=_line_of(text, m.start())))
        for m in _JS_IMPORT.finditer(text):
            out.append(Symbol(name=m.group(1), kind="import", file=rel_path, line=_line_of(text, m.start())))
        for m in _JS_REQUIRE.finditer(text):
            out.append(Symbol(name=m.group(1), kind="import", file=rel_path, line=_line_of(text, m.start())))
    elif suffix == ".go":
        for m in _GO_FUNC.finditer(text):
            out.append(Symbol(name=m.group(1), kind="function", file=rel_path, line=_line_of(text, m.start())))
        for m in _GO_TYPE.finditer(text):
            out.append(Symbol(name=m.group(1), kind="class", file=rel_path, line=_line_of(text, m.start())))
        for m in _GO_IMPORT.finditer(text):
            out.append(Symbol(name=m.group(1), kind="import", file=rel_path, line=_line_of(text, m.start())))
    elif suffix == ".rs":
        for m in _RS_FN.finditer(text):
            out.append(Symbol(name=m.group(1), kind="function", file=rel_path, line=_line_of(text, m.start())))
        for m in _RS_STRUCT.finditer(text):
            out.append(Symbol(name=m.group(1), kind="class", file=rel_path, line=_line_of(text, m.start())))
        for m in _RS_USE.finditer(text):
            out.append(Symbol(name=m.group(1).strip(), kind="import", file=rel_path, line=_line_of(text, m.start())))
    out.sort(key=lambda s: (s.file, s.line, s.kind, s.name))
    return out


def index_symbols(
    root: Union[str, Path],
    files: Union[list[str], None] = None,
    max_file_size: int = 1_000_000,
) -> dict[str, list[Symbol]]:
    """Index symbols for supported files under root (sorted keys)."""
    base = Path(root)
    if files is None:
        targets: list[str] = []
        for path in sorted(base.rglob("*")):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            if "." not in rel.rsplit("/", 1)[-1]:
                continue
            suffix = "." + rel.rsplit(".", 1)[-1].lower()
            if suffix in SUPPORTED_EXTENSIONS:
                targets.append(rel)
        targets.sort()
    else:
        targets = sorted(files)
    result: dict[str, list[Symbol]] = {}
    for rel in targets:
        suffix = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
        full = base / rel
        try:
            if full.stat().st_size > max_file_size:
                result[rel] = []
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            result[rel] = []
            continue
        result[rel] = discover_symbols_for_file(rel, text)
    return result
