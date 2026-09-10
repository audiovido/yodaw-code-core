"""Hermetic tests for symbol discovery and indexing."""

from pathlib import Path

from app.repo_intelligence.symbols import discover_symbols_for_file, index_symbols


def _write(base: Path, rel: str, content: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_python_defs_and_class():
    text = "def foo():\n    pass\n\nclass Bar:\n    def method(self):\n        pass\n"
    syms = discover_symbols_for_file("a.py", text)
    kinds = {(s.name, s.kind) for s in syms}
    assert ("foo", "function") in kinds
    assert ("Bar", "class") in kinds
    assert ("method", "function") in kinds


def test_python_imports():
    text = "import os, sys\nfrom foo import bar\n"
    syms = discover_symbols_for_file("a.py", text)
    imports = {s.name for s in syms if s.kind == "import"}
    assert {"os", "sys", "foo"} <= imports


def test_js_function_class_const():
    text = (
        "import x from './util';\n"
        "export function hello() {}\n"
        "export class World {}\n"
        "export const handler = async () => {};\n"
        "const r = require('./other');\n"
    )
    syms = discover_symbols_for_file("a.js", text)
    kinds = {(s.name, s.kind) for s in syms}
    assert ("hello", "function") in kinds
    assert ("World", "class") in kinds
    assert ("handler", "function") in kinds
    imports = {s.name for s in syms if s.kind == "import"}
    assert "./util" in imports and "./other" in imports


def test_ts_symbols():
    text = "export function run(): void {}\nexport class Store {}\n"
    syms = discover_symbols_for_file("a.ts", text)
    assert ("run", "function") in {(s.name, s.kind) for s in syms}
    assert ("Store", "class") in {(s.name, s.kind) for s in syms}


def test_go_symbols():
    text = 'package main\nimport "fmt"\nfunc main() {}\ntype Server struct{}\n'
    syms = discover_symbols_for_file("main.go", text)
    kinds = {(s.name, s.kind) for s in syms}
    assert ("main", "function") in kinds
    assert ("Server", "class") in kinds
    assert "fmt" in {s.name for s in syms if s.kind == "import"}


def test_rust_symbols():
    text = "use std::io;\npub fn main() {}\npub struct Config {}\n"
    syms = discover_symbols_for_file("main.rs", text)
    kinds = {(s.name, s.kind) for s in syms}
    assert ("main", "function") in kinds
    assert ("Config", "class") in kinds
    assert any(s.kind == "import" and "std::io" in s.name for s in syms)


def test_fallback_unsupported_extension():
    assert discover_symbols_for_file("notes.md", "# hi\n") == []
    assert discover_symbols_for_file("data.json", "{}\n") == []


def test_fallback_no_extension():
    assert discover_symbols_for_file("Makefile", "all:\n") == []


def test_line_numbers():
    syms = discover_symbols_for_file("a.py", "x = 1\ndef foo():\n    pass\n")
    foo = [s for s in syms if s.name == "foo"][0]
    assert foo.line == 2


def test_index_symbols_sorted_keys(tmp_path):
    _write(tmp_path, "b.py", "def b():\n    pass\n")
    _write(tmp_path, "a.py", "def a():\n    pass\n")
    result = index_symbols(tmp_path)
    assert list(result.keys()) == ["a.py", "b.py"]
    assert result["a.py"][0].name == "a"


def test_index_symbols_skips_unsupported(tmp_path):
    _write(tmp_path, "notes.md", "# hi\n")
    _write(tmp_path, "a.py", "def a():\n    pass\n")
    result = index_symbols(tmp_path)
    assert "notes.md" not in result
    assert "a.py" in result


def test_index_symbols_explicit_files(tmp_path):
    _write(tmp_path, "a.py", "def a():\n    pass\n")
    result = index_symbols(tmp_path, files=["a.py", "missing.py"])
    assert result["a.py"][0].name == "a"
    assert result["missing.py"] == []
