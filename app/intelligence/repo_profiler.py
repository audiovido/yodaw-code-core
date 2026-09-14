"""
Repository Profiler for YODAW Coder Intelligence Layer.

Extracts a compact machine-readable REPO PROFILE:
- Languages, frameworks, package/build tools
- Service boundaries, entrypoints, config patterns
- Test layout, toolchain (linters, formatters, type checkers)
- Dangerous files and generated code
- Conventions

Builds on the existing app.repo_intelligence evidence and adds
framework/tool detection. Never dumps whole repositories.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Set

from app.repo_intelligence.engine import RepoIntelligence
from app.repo_intelligence.models import Evidence


@dataclass
class RepoProfile:
    """Compact machine-readable repository profile."""

    repo_type: str
    primary_language: str
    secondary_languages: List[str] = field(default_factory=list)
    frameworks: List[str] = field(default_factory=list)
    build_tools: List[str] = field(default_factory=list)
    linters: List[str] = field(default_factory=list)
    formatters: List[str] = field(default_factory=list)
    type_checkers: List[str] = field(default_factory=list)
    service_boundaries: List[str] = field(default_factory=list)
    entrypoints: List[str] = field(default_factory=list)
    config_patterns: List[str] = field(default_factory=list)
    test_directories: List[str] = field(default_factory=list)
    has_ci: bool = False
    has_docker: bool = False
    dangerous_files: List[str] = field(default_factory=list)
    generated_code: List[str] = field(default_factory=list)
    conventions: dict = field(default_factory=dict)
    file_count: int = 0
    language_breakdown: dict = field(default_factory=dict)


def _match_globs(worktree: Path, patterns: List[str]) -> List[Path]:
    """Resolve literal paths and glob patterns to existing files."""
    found: List[Path] = []
    for pattern in patterns:
        if "*" in pattern:
            found.extend(worktree.glob(pattern))
        else:
            p = worktree / pattern
            if p.exists():
                found.append(p)
    return found


class RepoProfiler:
    """Profiles repository technology stack and conventions."""

    # language -> (framework key, marker fragments found in dependency/config files)
    FRAMEWORK_MARKERS: dict = {
        "python": {
            "django": ("django", "wsgi.py"),
            "flask": ("flask", "Flask"),
            "fastapi": ("fastapi", "FastAPI"),
            "sqlalchemy": ("sqlalchemy",),
            "pytest": ("pytest",),
        },
        "node": {
            "react": ("react", "react-dom"),
            "nextjs": ("next", "next.js"),
            "vue": ("vue",),
            "angular": ("@angular/core",),
            "svelte": ("svelte",),
            "express": ("express",),
            "fastify": ("fastify",),
            "jest": ("jest",),
            "vitest": ("vitest",),
            "playwright": ("@playwright", "playwright"),
            "typeorm": ("typeorm",),
        },
        "go": {
            "gin": ("gin-gonic",),
            "echo": ("labstack/echo",),
            "gorm": ("gorm.io/gorm",),
        },
        "rust": {
            "actix": ("actix-web",),
            "tokio": ("tokio",),
            "axum": ("axum",),
        },
        "java": {
            "spring": ("spring-boot", "springframework"),
            "hibernate": ("hibernate",),
            "junit": ("junit",),
        },
    }

    BUILD_MARKERS: dict = {
        "make": ("Makefile",),
        "cmake": ("CMakeLists.txt",),
        "maven": ("pom.xml",),
        "gradle": ("build.gradle", "build.gradle.kts"),
        "npm": ("package.json", "package-lock.json"),
        "yarn": ("yarn.lock",),
        "pnpm": ("pnpm-lock.yaml",),
        "pip": ("requirements.txt", "Pipfile", "poetry.lock"),
        "cargo": ("Cargo.toml", "Cargo.lock"),
        "go": ("go.mod", "go.sum"),
        "dotnet": ("*.csproj", "*.sln"),
    }

    LINTER_MARKERS: dict = {
        "ruff": ("ruff.toml", ".ruff.toml"),
        "flake8": (".flake8", "setup.cfg"),
        "pylint": (".pylintrc",),
        "mypy": ("mypy.ini", ".mypy.ini"),
        "pyright": ("pyrightconfig.json",),
        "eslint": (".eslintrc", ".eslintrc.js", ".eslintrc.json", "eslint.config.js"),
        "prettier": (".prettierrc", ".prettierrc.json", "prettier.config.js"),
        "clippy": (".clippy.toml",),
        "golangci": (".golangci.yml", ".golangci.yaml"),
    }

    ENTRYPOINTS: dict = {
        "python": ("main.py", "app.py", "manage.py", "wsgi.py", "asgi.py"),
        "node": ("index.js", "server.js", "app.js", "index.ts", "server.ts"),
        "go": ("main.go",),
        "rust": ("main.rs", "lib.rs"),
        "java": ("src/main/java",),
    }

    DANGEROUS_REL_PATTERNS = (
        ".env",
        ".env.local",
        ".env.production",
        ".netrc",
        ".npmrc",
        ".dockercfg",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
        "service-account.json",
        "secrets.yaml",
    )

    GENERATED_PATTERNS = (
        "*.pb.go",
        "*_pb2.py",
        "*.gen.go",
        "*.gen.ts",
        "*_generated.dart",
        "*.min.js",
        "*.lock",
    )

    TEST_DIRS = ("tests", "test", "__tests__", "spec", "specs", "e2e")

    SERVICE_DIR_NAMES = ("services", "microservices", "internal", "pkg", "api", "handlers", "controllers")

    CONFIG_FILE_NAMES = (
        "config.yaml",
        "config.yml",
        "config.json",
        "settings.py",
        "settings.yaml",
        "tsconfig.json",
        ".env.example",
    )

    IGNORED_DIRNAME = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "target",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "coverage",
        ".idea",
        ".vscode",
    }

    def __init__(self, max_file_size: int = 1_000_000):
        self.repo_intel = RepoIntelligence(max_file_size=max_file_size)

    def profile(self, worktree_path: str | Path) -> RepoProfile:
        worktree = Path(worktree_path)
        profile = RepoProfile(
            repo_type=self._detect_repo_type(worktree),
            primary_language="unknown",
        )
        try:
            evidence = self.repo_intel.analyze(worktree)
            profile.file_count = evidence.file_count
            profile.language_breakdown = dict(evidence.language_breakdown)
            profile.primary_language = self._primary_language(evidence)
            profile.secondary_languages = self._secondary_languages(evidence)
        except Exception:
            # Repo intelligence must never break profiling; degrade to
            # markers-only analysis.
            evidence = None

        self._detect_frameworks(worktree, profile)
        self._detect_build_tools(worktree, profile)
        self._detect_toolchain(worktree, profile)
        self._detect_architecture(worktree, profile)
        self._detect_quality(worktree, profile)
        self._detect_risk(worktree, profile)
        self._detect_conventions(worktree, profile, evidence)
        return profile

    # -- detection helpers -------------------------------------------------

    def _detect_repo_type(self, worktree: Path) -> str:
        from app.repo_intelligence.detector import detect_repo_type

        return detect_repo_type(worktree).value

    def _primary_language(self, evidence: Evidence) -> str:
        if not evidence.language_breakdown:
            return "unknown"
        # Config/markup languages never become "primary" when real
        # source code exists: an N-node package.json must not make a
        # TypeScript repo look like a JSON repo.
        config_langs = {"json", "toml", "yaml", "markdown", "unknown", ""}
        candidates = {
            lang: count
            for lang, count in evidence.language_breakdown.items()
            if lang not in config_langs
        }
        if not candidates:
            candidates = dict(evidence.language_breakdown)
        return max(candidates.items(), key=lambda kv: kv[1])[0]

    def _secondary_languages(self, evidence: Evidence) -> List[str]:
        if not evidence.language_breakdown:
            return []
        primary = self._primary_language(evidence)
        ordered = sorted(
            evidence.language_breakdown.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
        return [lang for lang, _ in ordered[1:4] if lang != primary]

    def _read_dependency_text(self, worktree: Path) -> str:
        """Concatenate package-manager files into one lowercase blob."""
        parts = []
        for pattern in (
            "requirements.txt",
            "Pipfile",
            "pyproject.toml",
            "package.json",
            "go.mod",
            "Cargo.toml",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
        ):
            p = worktree / pattern
            if p.is_file():
                try:
                    parts.append(p.read_text(errors="replace"))
                except OSError:
                    continue
        return "\n".join(parts).lower()

    # Map detected language to the framework marker group.
    LANGUAGE_TO_GROUP = {
        "python": "python",
        "node": "node",
        "nodejs": "node",
        "javascript": "node",
        "typescript": "node",
        "go": "go",
        "golang": "go",
        "rust": "rust",
        "java": "java",
    }

    def _framework_group(self, language: str) -> str:
        return self.LANGUAGE_TO_GROUP.get((language or "").lower(), "")

    def _detect_frameworks(self, worktree: Path, profile: RepoProfile) -> None:
        dep_text = self._read_dependency_text(worktree)
        markers = self.FRAMEWORK_MARKERS.get(
            self._framework_group(profile.primary_language), {}
        )
        found: Set[str] = set()
        for fw, fragments in markers.items():
            if any(fragment in dep_text for fragment in fragments):
                found.add(fw)
        # Also scan source files for additional framework signals so
        # frameworks not listed as direct dependencies are still caught
        # (e.g. a source file that `from fastapi import FastAPI`).
        source_text = self._read_source_snapshot(worktree, max_files=20, max_chars=50_000)
        if source_text:
            for fw, fragments in markers.items():
                if fw not in found and any(fragment in source_text for fragment in fragments):
                    found.add(fw)
        profile.frameworks = sorted(found)

    def _read_source_snapshot(self, worktree: Path, max_files: int = 20, max_chars: int = 50_000) -> str:
        """Scan the first N source files for framework marker strings."""
        source_suffixes = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".kts", ".go", ".rs", ".rb", ".php"}
        parts: list[str] = []
        count = 0
        for dirpath, dirnames, filenames in os.walk(worktree):
            dirnames[:] = sorted(d for d in dirnames if d not in self.IGNORED_DIRNAME)
            for name in sorted(filenames):
                if count >= max_files:
                    return "\n".join(parts)[:max_chars]
                path = Path(dirpath) / name
                if path.suffix.lower() not in source_suffixes:
                    continue
                try:
                    text = path.read_text(errors="replace")[:3000]
                    parts.append(text)
                except OSError:
                    continue
                count += 1
        return "\n".join(parts)[:max_chars]

    def _detect_build_tools(self, worktree: Path, profile: RepoProfile) -> None:
        found = set()
        for tool, markers in self.BUILD_MARKERS.items():
            if _match_globs(worktree, list(markers)):
                found.add(tool)
        profile.build_tools = sorted(found)

    def _detect_toolchain(self, worktree: Path, profile: RepoProfile) -> None:
        for tool, markers in self.LINTER_MARKERS.items():
            if _match_globs(worktree, list(markers)):
                if tool in ("mypy", "pyright"):
                    profile.type_checkers.append(tool)
                elif tool in ("prettier", "ruff"):
                    profile.formatters.append(tool)
                else:
                    profile.linters.append(tool)
        profile.linters = sorted(set(profile.linters))
        profile.formatters = sorted(set(profile.formatters))
        profile.type_checkers = sorted(set(profile.type_checkers))

    def _detect_architecture(self, worktree: Path, profile: RepoProfile) -> None:
        lang = profile.primary_language
        for entry in self.ENTRYPOINTS.get(lang, ()):
            if (worktree / entry).exists():
                profile.entrypoints.append(entry)
        for name in self.SERVICE_DIR_NAMES:
            if (worktree / name).is_dir():
                profile.service_boundaries.append(name)
        for name in self.CONFIG_FILE_NAMES:
            if (worktree / name).exists():
                profile.config_patterns.append(name)

    def _detect_quality(self, worktree: Path, profile: RepoProfile) -> None:
        for name in self.TEST_DIRS:
            if (worktree / name).is_dir():
                profile.test_directories.append(name)
        profile.has_ci = (worktree / ".github" / "workflows").is_dir() or (
            worktree / ".gitlab-ci.yml"
        ).exists()
        profile.has_docker = (worktree / "Dockerfile").exists() or (
            worktree / "docker-compose.yml"
        ).exists()

    def _walk_files(self, worktree: Path):
        for dirpath, dirnames, filenames in os.walk(worktree):
            dirnames[:] = sorted(d for d in dirnames if d not in self.IGNORED_DIRNAME)
            for name in filenames:
                yield Path(dirpath) / name

    def _detect_risk(self, worktree: Path, profile: RepoProfile) -> None:
        for path in self._walk_files(worktree):
            rel = path.relative_to(worktree)
            if path.is_symlink():
                continue
            if rel.name in self.DANGEROUS_REL_PATTERNS or any(
                p in rel.parts for p in ("node_modules",)
            ):
                if rel.name not in self.DANGEROUS_REL_PATTERNS:
                    continue
                profile.dangerous_files.append(rel.as_posix())
            for pattern in self.GENERATED_PATTERNS:
                if path.match(pattern):
                    profile.generated_code.append(rel.as_posix())
                    break
        profile.dangerous_files = sorted(set(profile.dangerous_files))[:20]
        profile.generated_code = sorted(set(profile.generated_code))[:20]

    def _detect_conventions(self, worktree: Path, profile: RepoProfile, evidence) -> None:
        profile.conventions = {
            "has_readme": any(
                (worktree / name).exists()
                for name in ("README.md", "README.rst", "README")
            ),
            "has_changelog": any(
                (worktree / name).exists()
                for name in ("CHANGELOG.md", "CHANGELOG", "HISTORY.md")
            ),
            "has_contributing": (worktree / "CONTRIBUTING.md").exists(),
            "has_pre_commit": (worktree / ".pre-commit-config.yaml").exists(),
        }
        if evidence is not None and evidence.tests:
            profile.conventions["test_count"] = len(evidence.tests)

    def to_dict(self, profile: RepoProfile) -> dict:
        dispatch = profile.dangerous_files[:10], profile.generated_code[:10]
        return {
            "repo_type": profile.repo_type,
            "primary_language": profile.primary_language,
            "secondary_languages": profile.secondary_languages,
            "frameworks": profile.frameworks,
            "build_tools": profile.build_tools,
            "linters": profile.linters,
            "formatters": profile.formatters,
            "type_checkers": profile.type_checkers,
            "service_boundaries": profile.service_boundaries,
            "entrypoints": profile.entrypoints,
            "config_patterns": profile.config_patterns,
            "test_directories": profile.test_directories,
            "has_ci": profile.has_ci,
            "has_docker": profile.has_docker,
            "dangerous_files": dispatch[0],
            "generated_code": dispatch[1],
            "conventions": profile.conventions,
            "file_count": profile.file_count,
            "language_breakdown": profile.language_breakdown,
        }


def profile_repository(worktree_path: str | Path) -> dict:
    """Convenience: profile a repository and return a JSON-shaped dict."""
    profiler = RepoProfiler()
    profile = profiler.profile(worktree_path)
    return profiler.to_dict(profile)


def format_repo_profile(profile_dict: dict, max_chars: int = 3000) -> str:
    """Render a compact profile as text for prompt injection."""
    parts = [
        f"REPOSITORY PROFILE (machine-detected):",
        f"  repo type: {profile_dict.get('repo_type')}",
        f"  primary language: {profile_dict.get('primary_language')}",
        (
            f"  secondary languages: {', '.join(profile_dict.get('secondary_languages') or [])}"
            if profile_dict.get("secondary_languages")
            else ""
        ),
        f"  frameworks: {', '.join(profile_dict.get('frameworks') or [])}",
        f"  build tools: {', '.join(profile_dict.get('build_tools') or [])}",
        (
            f"  linters: {', '.join(profile_dict.get('linters') or [])}"
            if profile_dict.get("linters")
            else ""
        ),
        (
            f"  formatters: {', '.join(profile_dict.get('formatters') or [])}"
            if profile_dict.get("formatters")
            else ""
        ),
        (
            f"  type checkers: {', '.join(profile_dict.get('type_checkers') or [])}"
            if profile_dict.get("type_checkers")
            else ""
        ),
        (
            f"  service boundaries: {', '.join(profile_dict.get('service_boundaries') or [])}"
            if profile_dict.get("service_boundaries")
            else ""
        ),
        (
            f"  entrypoints: {', '.join(profile_dict.get('entrypoints') or [])}"
            if profile_dict.get("entrypoints")
            else ""
        ),
        (
            f"  test dirs: {', '.join(profile_dict.get('test_directories') or [])}"
            if profile_dict.get("test_directories")
            else ""
        ),
        (
            f"  CI: {'yes' if profile_dict.get('has_ci') else 'no'}"
            f"; Docker: {'yes' if profile_dict.get('has_docker') else 'no'}"
        ),
    ]
    if profile_dict.get("dangerous_files"):
        parts.append(
            f"  DANGEROUS FILES (never edit unless task requires): "
            f"{', '.join(profile_dict.get('dangerous_files'))}"
        )
    if profile_dict.get("generated_code"):
        parts.append(
            f"  generated code (avoid editing): "
            f"{', '.join(profile_dict.get('generated_code'))}"
        )
    text = "\n".join(parts).strip()
    return text[:max_chars]