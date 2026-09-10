"""Hermetic tests for the RepoIntelligence facade and evidence output."""

import json
from pathlib import Path

from app.repo_intelligence import RepoIntelligence, build_evidence
from app.repo_intelligence.models import Evidence, RepoType


def _write(base: Path, rel: str, content: str = "x = 1\n") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _python_basic(base: Path) -> None:
    _write(base, "pyproject.toml", "[project]\nname = 'demo'\n")
    _write(base, "pkg/__init__.py", "")
    _write(base, "pkg/util.py", "def helper():\n    return 1\n")
    _write(base, "pkg/main.py", "from pkg.util import helper\n\ndef run():\n    return helper()\n")
    _write(base, "tests/test_util.py", "from pkg.util import helper\n\ndef test_helper():\n    assert helper() == 1\n")


def _node_basic(base: Path) -> None:
    _write(base, "package.json", '{"name": "demo"}\n')
    _write(base, "src/util.js", "export function helper() { return 1; }\n")
    _write(base, "src/main.js", "import {helper} from './util';\nhelper();\n")
    _write(base, "src/util.test.js", "import {helper} from './util';\n")


def _go_basic(base: Path) -> None:
    _write(base, "go.mod", "module demo\n")
    _write(base, "util.go", "package main\nfunc Helper() int { return 1 }\n")
    _write(base, "util_test.go", "package main\n")


def test_analyze_python_basic(tmp_path):
    _python_basic(tmp_path)
    ev = RepoIntelligence().analyze(tmp_path)
    assert ev.repo_type == RepoType.PYTHON
    assert ev.file_count == 5
    assert "pkg/main.py" in ev.symbols
    assert any(e.src == "pkg/main.py" and e.dst == "pkg/util.py" for e in ev.edges)
    assert "tests/test_util.py" in ev.tests
    assert ev.source_to_test.get("pkg/util.py") == "tests/test_util.py"


def test_analyze_node_basic(tmp_path):
    _node_basic(tmp_path)
    ev = RepoIntelligence().analyze(tmp_path)
    assert ev.repo_type == RepoType.NODE
    assert any(e.src == "src/main.js" and e.dst == "src/util.js" for e in ev.edges)


def test_analyze_go_basic(tmp_path):
    _go_basic(tmp_path)
    ev = RepoIntelligence().analyze(tmp_path)
    assert ev.repo_type == RepoType.GO
    assert "util_test.go" in ev.tests


def test_build_evidence_json_serializable(tmp_path):
    _python_basic(tmp_path)
    data = build_evidence(tmp_path)
    json.dumps(data)
    assert data["repo_type"] == "python"
    assert data["file_count"] == 5
    for key in ("files", "symbols", "edges", "tests", "source_to_test",
                "language_breakdown", "total_size", "skipped_files", "truncated"):
        assert key in data


def test_evidence_to_dict_repo_type_string(tmp_path):
    _python_basic(tmp_path)
    ev = RepoIntelligence().analyze(tmp_path)
    assert isinstance(ev, Evidence)
    assert ev.to_dict()["repo_type"] == "python"


def test_caching_returns_same_object(tmp_path):
    _python_basic(tmp_path)
    engine = RepoIntelligence()
    first = engine.analyze(tmp_path)
    second = engine.analyze(tmp_path)
    assert first is second


def test_invalidate_forces_recompute(tmp_path):
    _python_basic(tmp_path)
    engine = RepoIntelligence()
    first = engine.analyze(tmp_path)
    assert engine.invalidate(tmp_path) >= 1
    second = engine.analyze(tmp_path)
    assert second is not first
    assert second.to_dict() == first.to_dict()


def test_marks_test_flags(tmp_path):
    _python_basic(tmp_path)
    ev = RepoIntelligence().analyze(tmp_path)
    flags = {f.path: f.is_test for f in ev.files}
    assert flags["tests/test_util.py"] is True
    assert flags["pkg/util.py"] is False


def test_large_repo_safety_caps(tmp_path):
    _write(tmp_path, "pyproject.toml", "[project]\n")
    for i in range(20):
        _write(tmp_path, f"m{i:02d}.py", "x = 1\n")
    ev = RepoIntelligence(max_files=5).analyze(tmp_path)
    assert ev.file_count == 5
    assert ev.truncated is True
    assert ev.skipped_files == 16  # 21 total - 5 kept


def test_detect_static(tmp_path):
    _node_basic(tmp_path)
    assert RepoIntelligence.detect(tmp_path) == RepoType.NODE


def test_generic_repo(tmp_path):
    _write(tmp_path, "README.md", "# hi\n")
    ev = RepoIntelligence().analyze(tmp_path)
    assert ev.repo_type == RepoType.GENERIC
