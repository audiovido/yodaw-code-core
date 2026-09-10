"""
Tests for language and project adapters.
"""
import pytest
import tempfile
from pathlib import Path
from app.skills.adapters import (
    PythonAdapter,
    NodeAdapter,
    RustAdapter,
    GoAdapter,
    JavaAdapter,
    SwiftAdapter,
    detect_project_adapter,
)


def test_python_adapter_detect_pytest():
    """Test Python adapter detects pytest project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "pytest.ini").write_text("")
        
        adapter = PythonAdapter()
        assert adapter.detect(worktree) is True


def test_python_adapter_detect_requirements():
    """Test Python adapter detects requirements.txt."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "requirements.txt").write_text("pytest")
        
        adapter = PythonAdapter()
        assert adapter.detect(worktree) is True


def test_python_adapter_detect_py_files():
    """Test Python adapter detects .py files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "main.py").write_text("print('hello')")
        
        adapter = PythonAdapter()
        assert adapter.detect(worktree) is True


def test_python_adapter_test_commands():
    """Test Python adapter returns pytest commands."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "pytest.ini").write_text("")
        
        adapter = PythonAdapter()
        commands = adapter.test_commands(worktree)
        
        assert len(commands) == 1
        assert "pytest" in commands[0]


def test_python_adapter_source_paths():
    """Test Python adapter returns source paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "src").mkdir()
        (worktree / "app").mkdir()
        
        adapter = PythonAdapter()
        paths = adapter.source_paths(worktree)
        
        assert "src" in paths or "app" in paths


def test_python_adapter_dependency_files():
    """Test Python adapter returns dependency files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "requirements.txt").write_text("")
        (worktree / "pyproject.toml").write_text("")
        
        adapter = PythonAdapter()
        files = adapter.dependency_files(worktree)
        
        assert "requirements.txt" in files
        assert "pyproject.toml" in files


def test_node_adapter_detect():
    """Test Node adapter detects package.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "package.json").write_text('{"name": "test"}')
        
        adapter = NodeAdapter()
        assert adapter.detect(worktree) is True


def test_node_adapter_no_detect():
    """Test Node adapter doesn't detect non-node project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        
        adapter = NodeAdapter()
        assert adapter.detect(worktree) is False


def test_node_adapter_test_commands():
    """Test Node adapter returns npm test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "package.json").write_text('{"name": "test"}')
        
        adapter = NodeAdapter()
        commands = adapter.test_commands(worktree)
        
        assert len(commands) == 1
        assert "npm" in commands[0]
        assert "test" in commands[0]


def test_rust_adapter_detect():
    """Test Rust adapter detects Cargo.toml."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "Cargo.toml").write_text('[package]\nname = "test"')
        
        adapter = RustAdapter()
        assert adapter.detect(worktree) is True


def test_rust_adapter_test_commands():
    """Test Rust adapter returns cargo test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "Cargo.toml").write_text('[package]\nname = "test"')
        
        adapter = RustAdapter()
        commands = adapter.test_commands(worktree)
        
        assert len(commands) == 1
        assert "cargo" in commands[0]
        assert "test" in commands[0]


def test_go_adapter_detect():
    """Test Go adapter detects go.mod."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "go.mod").write_text('module test')
        
        adapter = GoAdapter()
        assert adapter.detect(worktree) is True


def test_go_adapter_test_commands():
    """Test Go adapter returns go test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "go.mod").write_text('module test')
        
        adapter = GoAdapter()
        commands = adapter.test_commands(worktree)
        
        assert len(commands) == 1
        assert "go" in commands[0]
        assert "test" in commands[0]


def test_java_adapter_detect_maven():
    """Test Java adapter detects Maven project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "pom.xml").write_text('<project></project>')
        
        adapter = JavaAdapter()
        assert adapter.detect(worktree) is True


def test_java_adapter_detect_gradle():
    """Test Java adapter detects Gradle project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "build.gradle").write_text('')
        
        adapter = JavaAdapter()
        assert adapter.detect(worktree) is True


def test_java_adapter_test_commands_maven():
    """Test Java adapter returns mvn test for Maven."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "pom.xml").write_text('<project></project>')
        
        adapter = JavaAdapter()
        commands = adapter.test_commands(worktree)
        
        assert len(commands) == 1
        assert "mvn" in commands[0]
        assert "test" in commands[0]


def test_swift_adapter_detect():
    """Test Swift adapter detects Package.swift."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "Package.swift").write_text('')
        
        adapter = SwiftAdapter()
        assert adapter.detect(worktree) is True


def test_swift_adapter_test_commands():
    """Test Swift adapter returns swift test."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "Package.swift").write_text('')
        
        adapter = SwiftAdapter()
        commands = adapter.test_commands(worktree)
        
        assert len(commands) == 1
        assert "swift" in commands[0]
        assert "test" in commands[0]


def test_detect_project_adapter_python():
    """Test detect_project_adapter finds Python adapter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "pytest.ini").write_text("")
        
        adapter = detect_project_adapter(worktree)
        
        assert adapter is not None
        assert adapter.language == "python"


def test_detect_project_adapter_node():
    """Test detect_project_adapter finds Node adapter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "package.json").write_text('{}')
        
        adapter = detect_project_adapter(worktree)
        
        assert adapter is not None
        assert adapter.language == "node"


def test_detect_project_adapter_rust():
    """Test detect_project_adapter finds Rust adapter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        (worktree / "Cargo.toml").write_text('')
        
        adapter = detect_project_adapter(worktree)
        
        assert adapter is not None
        assert adapter.language == "rust"


def test_detect_project_adapter_none():
    """Test detect_project_adapter returns None for unknown project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        worktree = Path(tmpdir)
        
        adapter = detect_project_adapter(worktree)
        
        assert adapter is None


def test_adapter_language_property():
    """Test all adapters have language property."""
    adapters = [
        PythonAdapter(),
        NodeAdapter(),
        RustAdapter(),
        GoAdapter(),
        JavaAdapter(),
        SwiftAdapter(),
    ]
    
    for adapter in adapters:
        assert adapter.language
        assert isinstance(adapter.language, str)
