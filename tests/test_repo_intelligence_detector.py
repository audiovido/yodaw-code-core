"""Hermetic tests for repo type detection and repo map building."""

from pathlib import Path

from app.repo_intelligence.detector import (
    build_repo_map,
    detect_repo_type,
    find_marker,
    list_repo_files,
)
from app.repo_intelligence.models import RepoType


def _touch(base: Path, rel: str, content: str = "x = 1\n") -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_detect_python_pyproject(tmp_path):
    _touch(tmp_path, "pyproject.toml", "[project]\n")
    assert detect_repo_type(tmp_path) == RepoType.PYTHON


def test_detect_python_requirements(tmp_path):
    _touch(tmp_path, "requirements.txt", "requests\n")
    assert detect_repo_type(tmp_path) == RepoType.PYTHON


def test_detect_python_setup_py(tmp_path):
    _touch(tmp_path, "setup.py", "from setuptools import setup\n")
    assert detect_repo_type(tmp_path) == RepoType.PYTHON


def test_detect_node(tmp_path):
    _touch(tmp_path, "package.json", "{}\n")
    assert detect_repo_type(tmp_path) == RepoType.NODE


def test_detect_go(tmp_path):
    _touch(tmp_path, "go.mod", "module example\n")
    assert detect_repo_type(tmp_path) == RepoType.GO


def test_detect_rust(tmp_path):
    _touch(tmp_path, "Cargo.toml", "[package]\n")
    assert detect_repo_type(tmp_path) == RepoType.RUST


def test_detect_generic_empty(tmp_path):
    assert detect_repo_type(tmp_path) == RepoType.GENERIC


def test_detect_priority_python_over_node(tmp_path):
    _touch(tmp_path, "pyproject.toml", "[project]\n")
    _touch(tmp_path, "package.json", "{}\n")
    assert detect_repo_type(tmp_path) == RepoType.PYTHON


def test_find_marker(tmp_path):
    assert find_marker(tmp_path) is None
    _touch(tmp_path, "go.mod", "module x\n")
    assert find_marker(tmp_path) == "go.mod"


def test_build_repo_map_python_basic(tmp_path):
    _touch(tmp_path, "pyproject.toml", "[project]\n")
    _touch(tmp_path, "src/app.py", "x = 1\n")
    _touch(tmp_path, "src/util.py", "y = 2\n")
    repo_map = build_repo_map(tmp_path)
    assert repo_map.repo_type == RepoType.PYTHON
    assert repo_map.language_breakdown.get("python") == 2
    paths = [f.path for f in repo_map.files]
    assert paths == sorted(paths)
    assert "src/app.py" in paths


def test_build_repo_map_sizes(tmp_path):
    _touch(tmp_path, "a.py", "12345")
    repo_map = build_repo_map(tmp_path)
    assert repo_map.total_size == 5
    assert repo_map.files[0].size == 5


def test_skip_git_venv_node_modules_target(tmp_path):
    _touch(tmp_path, "keep.py", "x=1\n")
    _touch(tmp_path, ".git/HEAD", "ref\n")
    _touch(tmp_path, "venv/lib/x.py", "x=1\n")
    _touch(tmp_path, "node_modules/pkg/index.js", "x=1\n")
    _touch(tmp_path, "target/debug/bin", "x=1\n")
    _touch(tmp_path, "__pycache__/a.pyc", "x=1\n")
    nodes, skipped, _ = list_repo_files(tmp_path)
    assert [n.path for n in nodes] == ["keep.py"]
    assert skipped == 5


def test_max_files_cap(tmp_path):
    for i in range(10):
        _touch(tmp_path, f"f{i:02d}.py", "x=1\n")
    nodes, _, truncated = list_repo_files(tmp_path, max_files=4)
    assert len(nodes) == 4
    assert truncated is True


def test_max_file_size_skip(tmp_path):
    _touch(tmp_path, "small.py", "x=1\n")
    _touch(tmp_path, "big.py", "x" * 100)
    nodes, skipped, _ = list_repo_files(tmp_path, max_file_size=10)
    assert [n.path for n in nodes] == ["small.py"]
    assert skipped == 1


def test_binary_skip(tmp_path):
    p = tmp_path / "img.png"
    p.write_bytes(b"\x89PNG\x00\x01\x02")
    _touch(tmp_path, "ok.py", "x=1\n")
    nodes, skipped, _ = list_repo_files(tmp_path)
    assert [n.path for n in nodes] == ["ok.py"]
    assert skipped == 1


def test_deterministic_ordering(tmp_path):
    for name in ("z.py", "a.py", "m.py"):
        _touch(tmp_path, name, "x=1\n")
    first = [n.path for n in list_repo_files(tmp_path)[0]]
    second = [n.path for n in list_repo_files(tmp_path)[0]]
    assert first == ["a.py", "m.py", "z.py"] == second
