"""Hermetic tests for the import/dependency graph."""

from pathlib import Path

from app.repo_intelligence.graph import (
    build_import_graph,
    extract_import_specs,
    forward_dependencies,
    resolve_spec,
    reverse_dependencies,
)


def _write(base: Path, rel: str, content: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_python_absolute_import_resolved(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/util.py", "X = 1\n")
    _write(tmp_path, "main.py", "import pkg.util\n")
    edges = build_import_graph(tmp_path, ["main.py", "pkg/__init__.py", "pkg/util.py"])
    assert any(e.src == "main.py" and e.dst == "pkg/util.py" for e in edges)


def test_python_from_import_resolved(tmp_path):
    _write(tmp_path, "util.py", "X = 1\n")
    _write(tmp_path, "main.py", "from util import X\n")
    edges = build_import_graph(tmp_path, ["main.py", "util.py"])
    assert any(e.src == "main.py" and e.dst == "util.py" for e in edges)


def test_python_relative_import(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "from . import b\n")
    _write(tmp_path, "pkg/b.py", "X = 1\n")
    edges = build_import_graph(tmp_path, ["pkg/__init__.py", "pkg/a.py", "pkg/b.py"])
    assert any(e.src == "pkg/a.py" and e.dst == "pkg/b.py" for e in edges)


def test_js_relative_import(tmp_path):
    _write(tmp_path, "util.js", "export const x = 1;\n")
    _write(tmp_path, "main.js", "import {x} from './util';\n")
    edges = build_import_graph(tmp_path, ["main.js", "util.js"])
    assert any(e.src == "main.js" and e.dst == "util.js" for e in edges)


def test_js_require(tmp_path):
    _write(tmp_path, "other.js", "module.exports = {};\n")
    _write(tmp_path, "main.js", "const o = require('./other');\n")
    edges = build_import_graph(tmp_path, ["main.js", "other.js"])
    assert any(e.src == "main.js" and e.dst == "other.js" for e in edges)


def test_unresolvable_keeps_raw(tmp_path):
    _write(tmp_path, "main.py", "import requests\n")
    edges = build_import_graph(tmp_path, ["main.py"])
    assert ("main.py", "requests", "requests") in [(e.src, e.dst, e.raw) for e in edges]


def test_sorted_dedup(tmp_path):
    _write(tmp_path, "b.py", "import os\nimport os\n")
    edges = build_import_graph(tmp_path, ["b.py"])
    keys = [(e.src, e.dst, e.raw) for e in edges]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_reverse_dependencies():
    from app.repo_intelligence.models import ImportEdge
    edges = [
        ImportEdge(src="a.py", dst="util.py", raw="util"),
        ImportEdge(src="b.py", dst="util.py", raw="util"),
    ]
    assert reverse_dependencies(edges) == {"util.py": ["a.py", "b.py"]}


def test_forward_dependencies():
    from app.repo_intelligence.models import ImportEdge
    edges = [
        ImportEdge(src="a.py", dst="x.py", raw="x"),
        ImportEdge(src="a.py", dst="y.py", raw="y"),
    ]
    assert forward_dependencies(edges) == {"a.py": ["x.py", "y.py"]}


def test_extract_specs_go_and_rust():
    go = 'package m\nimport (\n"fmt"\n"m/util"\n)\n'
    assert "fmt" in extract_import_specs("a.go", go)
    rs = "use std::io;\nfn main() {}\n"
    assert "std::io" in extract_import_specs("a.rs", rs)


def test_resolve_spec_unknown_suffix():
    assert resolve_spec("notes.md", "foo", {"foo"}) == "foo"
