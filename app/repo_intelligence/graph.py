"""File-level import/dependency graph construction."""

import re
from pathlib import Path, PurePosixPath
from typing import Union

from .models import ImportEdge

_PY_IMPORT_RE = re.compile(r"^\s*import\s+(.+)$", re.M)
_PY_FROM_RE = re.compile(r"^\s*from\s+(\.+)?(\S*)\s+import\s+(.+)$", re.M)
_JS_IMPORT_RE = re.compile(r"^\s*import\s+(?:.+?\s+from\s+)?['\"]([^'\"]+)['\"]", re.M)
_JS_REQUIRE_RE = re.compile(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", re.M)
_GO_IMPORT_BLOCK = re.compile(r"import\s*\(\s*(.*?)\s*\)", re.S)
_GO_IMPORT_ONE = re.compile(r'^\s*import\s+(?:[A-Za-z_\.]\w*\s+)?"([^"]+)"', re.M)
_GO_QUOTED = re.compile(r'"([^"]+)"')
_RS_USE_RE = re.compile(r"^\s*use\s+([^;]+);", re.M)


def _resolve_relative(src: str, spec: str) -> Union[str, None]:
    """Resolve a relative import spec to a repo-relative path, or None."""
    if not spec.startswith("."):
        return None
    base = PurePosixPath(src).parent
    target = (base / spec)
    candidates = [target.as_posix()]
    for cand in (target.as_posix(),):
        for ext in (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs"):
            candidates.append(cand + ext)
        candidates.append(cand + "/__init__.py")
        candidates.append(cand + "/index.js")
        candidates.append(cand + "/index.ts")
        candidates.append(cand + "/mod.rs")
    return candidates[0]


def _resolve_python(src: str, module: str, known: set[str]) -> Union[str, None]:
    """Resolve a python module path against known files."""
    rel = module.replace(".", "/")
    for cand in (rel + ".py", rel + "/__init__.py"):
        if cand in known:
            return cand
    return None


def _resolve_js(src: str, spec: str, known: set[str]) -> Union[str, None]:
    """Resolve a JS/TS spec against known files."""
    if spec.startswith("."):
        base = PurePosixPath(src).parent
        target = (base / spec).as_posix()
        stems = [target]
    else:
        return None
    for stem in stems:
        if stem in known:
            return stem
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            if stem + ext in known:
                return stem + ext
        for idx in ("index.ts", "index.tsx", "index.js", "index.jsx"):
            if stem + "/" + idx in known:
                return stem + "/" + idx
    return None


def extract_import_specs(rel_path: str, text: str) -> list[str]:
    """Extract raw import specs from file text."""
    suffix = "." + rel_path.rsplit(".", 1)[-1].lower() if "." in rel_path.rsplit("/", 1)[-1] else ""
    specs: list[str] = []
    if suffix == ".py":
        for m in _PY_IMPORT_RE.finditer(text):
            for part in m.group(1).split(","):
                name = part.strip().split(" as ")[0].strip()
                if name:
                    specs.append(name)
        for m in _PY_FROM_RE.finditer(text):
            dots, mod, names = m.group(1) or "", m.group(2) or "", m.group(3) or ""
            if dots and not mod:
                for part in names.split(","):
                    name = part.strip().split(" as ")[0].strip()
                    if name and name != "*":
                        specs.append(dots + name)
            elif dots:
                specs.append(dots + mod)
            elif mod:
                specs.append(mod)
    elif suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
        specs.extend(_JS_IMPORT_RE.findall(text))
        specs.extend(_JS_REQUIRE_RE.findall(text))
    elif suffix == ".go":
        for m in _GO_IMPORT_BLOCK.finditer(text):
            specs.extend(_GO_QUOTED.findall(m.group(1)))
        specs.extend(_GO_IMPORT_ONE.findall(text))
    elif suffix == ".rs":
        for m in _RS_USE_RE.finditer(text):
            specs.append(m.group(1).strip())
    return specs


def resolve_spec(src: str, spec: str, known: set[str]) -> str:
    """Resolve a spec to a known path, else return the raw spec."""
    suffix = "." + src.rsplit(".", 1)[-1].lower() if "." in src.rsplit("/", 1)[-1] else ""
    resolved: Union[str, None] = None
    if suffix == ".py":
        if spec.startswith("."):
            base = PurePosixPath(src).parent
            dots = len(spec) - len(spec.lstrip("."))
            rest = spec.lstrip(".")
            parent = base
            for _ in range(dots - 1):
                parent = parent.parent
            rel = (parent / rest.replace(".", "/")).as_posix() if rest else parent.as_posix()
            for cand in (rel + ".py", rel + "/__init__.py"):
                if cand in known:
                    resolved = cand
                    break
        else:
            resolved = _resolve_python(src, spec.split(" as ")[0].strip(), known)
    elif suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
        resolved = _resolve_js(src, spec, known)
    elif suffix in (".go", ".rs"):
        resolved = spec if spec in known else None
    return resolved or spec


def build_import_graph(
    root: Union[str, Path],
    files: Union[list[str], None] = None,
    max_file_size: int = 1_000_000,
) -> list[ImportEdge]:
    """Build file-level import edges (sorted, deduplicated)."""
    base = Path(root)
    if files is None:
        targets = set()
        for path in sorted(base.rglob("*")):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            rel = path.relative_to(base).as_posix()
            targets.add(rel)
        file_list = sorted(targets)
    else:
        file_list = sorted(files)
    known = set(file_list)
    edges: dict[tuple[str, str, str], ImportEdge] = {}
    for rel in file_list:
        full = base / rel
        try:
            if full.stat().st_size > max_file_size:
                continue
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for spec in extract_import_specs(rel, text):
            dst = resolve_spec(rel, spec, known)
            key = (rel, dst, spec)
            edges[key] = ImportEdge(src=rel, dst=dst, raw=spec)
    return sorted(edges.values(), key=lambda e: (e.src, e.dst, e.raw))


def reverse_dependencies(edges: list[ImportEdge]) -> dict[str, list[str]]:
    """Map each file to the sorted list of files importing it."""
    rev: dict[str, set[str]] = {}
    for edge in edges:
        rev.setdefault(edge.dst, set()).add(edge.src)
    return {k: sorted(v) for k, v in sorted(rev.items())}


def forward_dependencies(edges: list[ImportEdge]) -> dict[str, list[str]]:
    """Map each file to the sorted list of files it imports."""
    fwd: dict[str, set[str]] = {}
    for edge in edges:
        fwd.setdefault(edge.src, set()).add(edge.dst)
    return {k: sorted(v) for k, v in sorted(fwd.items())}
