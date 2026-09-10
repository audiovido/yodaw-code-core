"""
Language and project adapters for project detection and tooling.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


@dataclass
class ToolAvailability:
    """Tool detection result."""
    available: bool
    reason: str = ""


@runtime_checkable
class ProjectAdapter(Protocol):
    """
    Language/ecosystem adapter for project-specific tooling detection.
    """
    
    @property
    def language(self) -> str:
        """Language name."""
        ...
    
    def detect(self, worktree: Path) -> bool:
        """Returns True if this adapter applies to the project."""
        ...
    
    def source_paths(self, worktree: Path) -> list:
        """Likely source file paths."""
        ...
    
    def test_commands(self, worktree: Path) -> list:
        """Test commands if detectable."""
        ...
    
    def lint_commands(self, worktree: Path) -> list:
        """Lint commands if detectable."""
        ...
    
    def format_commands(self, worktree: Path) -> list:
        """Format commands if detectable."""
        ...
    
    def build_commands(self, worktree: Path) -> list:
        """Build commands if detectable."""
        ...
    
    def dependency_files(self, worktree: Path) -> list:
        """Package/dependency manifest files."""
        ...


class PythonAdapter:
    """Python project adapter."""
    
    @property
    def language(self) -> str:
        return "python"
    
    def detect(self, worktree: Path) -> bool:
        indicators = [
            "setup.py",
            "pyproject.toml",
            "requirements.txt",
            "Pipfile",
            "pytest.ini",
            "setup.cfg",
        ]
        
        for name in indicators:
            if (worktree / name).exists():
                return True
        
        # Check for .py files
        py_files = list(worktree.glob("**/*.py"))
        return len(py_files) > 0
    
    def source_paths(self, worktree: Path) -> list:
        paths = []
        
        for candidate in ["src", "app", "lib", "."]:
            path = worktree / candidate
            if path.exists() and path.is_dir():
                paths.append(candidate)
        
        return paths or ["."]
    
    def test_commands(self, worktree: Path) -> list:
        if (worktree / "pytest.ini").exists() or (worktree / "tests").exists():
            return [["python", "-m", "pytest", "-q"]]
        
        if (worktree / "setup.py").exists():
            return [["python", "setup.py", "test"]]
        
        return []
    
    def lint_commands(self, worktree: Path) -> list:
        commands = []
        
        if (worktree / ".flake8").exists() or (worktree / "setup.cfg").exists():
            commands.append(["flake8", "."])
        
        if (worktree / "pyproject.toml").exists():
            commands.append(["ruff", "check", "."])
        
        return commands
    
    def format_commands(self, worktree: Path) -> list:
        if (worktree / "pyproject.toml").exists():
            return [["black", "."]]
        
        return []
    
    def build_commands(self, worktree: Path) -> list:
        if (worktree / "setup.py").exists():
            return [["python", "setup.py", "build"]]
        
        if (worktree / "pyproject.toml").exists():
            return [["python", "-m", "build"]]
        
        return []
    
    def dependency_files(self, worktree: Path) -> list:
        files = []
        
        for name in ["requirements.txt", "pyproject.toml", "setup.py", "Pipfile"]:
            if (worktree / name).exists():
                files.append(name)
        
        return files


class NodeAdapter:
    """Node.js/TypeScript project adapter."""
    
    @property
    def language(self) -> str:
        return "node"
    
    def detect(self, worktree: Path) -> bool:
        return (worktree / "package.json").exists()
    
    def source_paths(self, worktree: Path) -> list:
        paths = []
        
        for candidate in ["src", "lib", "app", "."]:
            path = worktree / candidate
            if path.exists() and path.is_dir():
                paths.append(candidate)
        
        return paths or ["."]
    
    def test_commands(self, worktree: Path) -> list:
        return [["npm", "test"]]
    
    def lint_commands(self, worktree: Path) -> list:
        if (worktree / ".eslintrc.js").exists() or (worktree / ".eslintrc.json").exists():
            return [["npm", "run", "lint"]]
        
        return []
    
    def format_commands(self, worktree: Path) -> list:
        if (worktree / ".prettierrc").exists() or (worktree / "prettier.config.js").exists():
            return [["npm", "run", "format"]]
        
        return []
    
    def build_commands(self, worktree: Path) -> list:
        package_json = worktree / "package.json"
        
        if package_json.exists():
            import json
            try:
                data = json.loads(package_json.read_text())
                if "build" in data.get("scripts", {}):
                    return [["npm", "run", "build"]]
            except Exception:
                pass
        
        return []
    
    def dependency_files(self, worktree: Path) -> list:
        return ["package.json", "package-lock.json"]


class RustAdapter:
    """Rust project adapter."""
    
    @property
    def language(self) -> str:
        return "rust"
    
    def detect(self, worktree: Path) -> bool:
        return (worktree / "Cargo.toml").exists()
    
    def source_paths(self, worktree: Path) -> list:
        return ["src"]
    
    def test_commands(self, worktree: Path) -> list:
        return [["cargo", "test"]]
    
    def lint_commands(self, worktree: Path) -> list:
        return [["cargo", "clippy"]]
    
    def format_commands(self, worktree: Path) -> list:
        return [["cargo", "fmt"]]
    
    def build_commands(self, worktree: Path) -> list:
        return [["cargo", "build"]]
    
    def dependency_files(self, worktree: Path) -> list:
        return ["Cargo.toml", "Cargo.lock"]


class GoAdapter:
    """Go project adapter."""
    
    @property
    def language(self) -> str:
        return "go"
    
    def detect(self, worktree: Path) -> bool:
        return (worktree / "go.mod").exists()
    
    def source_paths(self, worktree: Path) -> list:
        paths = []
        
        for candidate in [".", "cmd", "pkg", "internal"]:
            path = worktree / candidate
            if path.exists() and path.is_dir():
                paths.append(candidate)
        
        return paths or ["."]
    
    def test_commands(self, worktree: Path) -> list:
        return [["go", "test", "./..."]]
    
    def lint_commands(self, worktree: Path) -> list:
        return [["golint", "./..."]]
    
    def format_commands(self, worktree: Path) -> list:
        return [["go", "fmt", "./..."]]
    
    def build_commands(self, worktree: Path) -> list:
        return [["go", "build", "./..."]]
    
    def dependency_files(self, worktree: Path) -> list:
        return ["go.mod", "go.sum"]


class JavaAdapter:
    """Java project adapter."""
    
    @property
    def language(self) -> str:
        return "java"
    
    def detect(self, worktree: Path) -> bool:
        return (
            (worktree / "pom.xml").exists()
            or (worktree / "build.gradle").exists()
            or (worktree / "build.gradle.kts").exists()
        )
    
    def source_paths(self, worktree: Path) -> list:
        return ["src/main/java", "src"]
    
    def test_commands(self, worktree: Path) -> list:
        if (worktree / "pom.xml").exists():
            return [["mvn", "test"]]
        
        if (worktree / "build.gradle").exists() or (worktree / "build.gradle.kts").exists():
            return [["gradle", "test"]]
        
        return []
    
    def lint_commands(self, worktree: Path) -> list:
        return []
    
    def format_commands(self, worktree: Path) -> list:
        return []
    
    def build_commands(self, worktree: Path) -> list:
        if (worktree / "pom.xml").exists():
            return [["mvn", "compile"]]
        
        if (worktree / "build.gradle").exists() or (worktree / "build.gradle.kts").exists():
            return [["gradle", "build"]]
        
        return []
    
    def dependency_files(self, worktree: Path) -> list:
        files = []
        
        for name in ["pom.xml", "build.gradle", "build.gradle.kts"]:
            if (worktree / name).exists():
                files.append(name)
        
        return files


class SwiftAdapter:
    """Swift project adapter."""
    
    @property
    def language(self) -> str:
        return "swift"
    
    def detect(self, worktree: Path) -> bool:
        return (worktree / "Package.swift").exists()
    
    def source_paths(self, worktree: Path) -> list:
        return ["Sources"]
    
    def test_commands(self, worktree: Path) -> list:
        return [["swift", "test"]]
    
    def lint_commands(self, worktree: Path) -> list:
        return [["swiftlint"]]
    
    def format_commands(self, worktree: Path) -> list:
        return [["swift-format", "format", "-i", "-r", "."]]
    
    def build_commands(self, worktree: Path) -> list:
        return [["swift", "build"]]
    
    def dependency_files(self, worktree: Path) -> list:
        return ["Package.swift", "Package.resolved"]


# Registry of all adapters
ALL_ADAPTERS = [
    PythonAdapter(),
    NodeAdapter(),
    RustAdapter(),
    GoAdapter(),
    JavaAdapter(),
    SwiftAdapter(),
]


def detect_project_adapter(worktree: Path) -> Optional[ProjectAdapter]:
    """
    Detect the appropriate project adapter for the given worktree.
    Returns None if no adapter matches.
    """
    for adapter in ALL_ADAPTERS:
        if adapter.detect(worktree):
            return adapter
    
    return None
