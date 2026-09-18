"""Repository intelligence for the Grok Architect.

The architect must decide from *real* facts about the repository, not
from the goal string alone. Everything assembled here comes from code
the project already ships:

- ``app.repo_intelligence.RepoIntelligence`` for repo type, language
  breakdown, file inventory, symbols, and discovered tests
- ``app.workers.validation.detect_test_commands`` for the actual
  commands this repository would run for validation
- marker files (package.json / pyproject.toml / go.mod ...) for
  framework and tool detection

The payload is bounded: the architect never receives a whole repo
dump, only a shaped brief.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

MAX_FILES_LISTED = 40
MAX_MARKER_BYTES = 20_000

FRAMEWORK_MARKERS = {
    "package.json": ("react", "vue", "svelte", "next", "vite", "express", "fastify"),
    "pyproject.toml": ("fastapi", "django", "flask", "pytest", "poetry"),
    "requirements.txt": ("fastapi", "django", "flask", "uvicorn"),
    "go.mod": ("gin", "echo", "fiber"),
    "Cargo.toml": ("actix", "axum", "tokio"),
}

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".swift": "swift",
    ".sql": "sql",
    ".sh": "bash",
    ".css": "css",
    ".html": "html",
}


def collect_repo_intel(repo: Optional[str]) -> dict[str, Any]:
    """Bounded, JSON-serializable repository brief for the planner."""
    if not repo:
        return {"available": False, "reason": "no repository supplied"}

    path = Path(repo)
    if not (path / ".git").exists():
        return {
            "available": False,
            "reason": f"{repo} is not a git repository",
            "path": str(path),
        }

    intel: dict[str, Any] = {"available": True, "path": str(path)}

    try:
        from app.repo_intelligence import RepoIntelligence

        evidence = RepoIntelligence().build_evidence(str(path))
        intel["repo_type"] = evidence.get("repo_type")
        intel["file_count"] = evidence.get("file_count")
        breakdown = evidence.get("language_breakdown") or {}
        intel["language_breakdown"] = _top(breakdown, 8)
        languages = [
            LANGUAGE_BY_EXT[Path(entry.get("path", "")).suffix.lower()]
            for entry in (evidence.get("files") or [])[:400]
            if Path(entry.get("path", "")).suffix.lower() in LANGUAGE_BY_EXT
        ]
        intel["languages"] = sorted(set(languages))
        intel["tests"] = [
            entry.get("path") for entry in (evidence.get("tests") or [])[:20]
        ]
        intel["files"] = [
            entry.get("path") for entry in (evidence.get("files") or [])[:MAX_FILES_LISTED]
        ]
    except Exception as exc:
        intel["intelligence_error"] = str(exc)[:300]

    intel["frameworks"] = _detect_frameworks(path)
    intel["tools"] = _detect_tools(path)
    intel["package_metadata"] = _package_metadata(path)
    intel["git"] = _git_facts(path)
    intel["validation_commands"] = _validation_commands(path)
    intel["available_executors"] = _executor_brief()

    return intel


def _top(mapping: dict, limit: int) -> dict:
    try:
        ordered = sorted(mapping.items(), key=lambda item: item[1], reverse=True)
    except Exception:
        return {}
    return {key: value for key, value in ordered[:limit]}


def _detect_frameworks(repo: Path) -> list[str]:
    found: set[str] = set()
    for marker, names in FRAMEWORK_MARKERS.items():
        target = repo / marker
        if not target.is_file():
            continue
        text = _read_bounded(target).lower()
        for name in names:
            if name in text:
                found.add(name)
    return sorted(found)


def _detect_tools(repo: Path) -> list[str]:
    tools = ["git"]
    markers = {
        "package.json": "npm",
        "pyproject.toml": "python",
        "requirements.txt": "python",
        "pytest.ini": "pytest",
        "go.mod": "go",
        "Cargo.toml": "cargo",
        "pom.xml": "maven",
        "build.gradle": "gradle",
        "Dockerfile": "docker",
        "Makefile": "make",
    }
    for marker, tool in markers.items():
        if (repo / marker).exists():
            tools.append(tool)
    return sorted(set(tools))


def _package_metadata(repo: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    package = repo / "package.json"
    if package.is_file():
        try:
            data = json.loads(_read_bounded(package))
            payload["package.json"] = {
                "name": data.get("name"),
                "scripts": data.get("scripts") or {},
                "dependencies": sorted((data.get("dependencies") or {}).keys())[:40],
                "devDependencies": sorted(
                    (data.get("devDependencies") or {}).keys()
                )[:40],
            }
        except Exception as exc:
            payload["package.json_error"] = str(exc)[:200]
    for marker in ("pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml"):
        if (repo / marker).is_file():
            payload[marker] = _read_bounded(repo / marker)[:4000]
    return payload


def _git_facts(repo: Path) -> dict[str, Any]:
    from app.workers.safe_subprocess import run

    facts: dict[str, Any] = {}
    for key, args in (
        ("head", ["git", "rev-parse", "HEAD"]),
        ("branch", ["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        ("dirty", ["git", "status", "--porcelain"]),
    ):
        result = run(args, cwd=str(repo), timeout=30)
        if key == "dirty":
            facts["dirty"] = bool((result.get("stdout") or "").strip())
        else:
            facts[key] = (result.get("stdout") or "").strip()
    return facts


def _validation_commands(repo: Path) -> list[list[str]]:
    try:
        from app.workers.validation import detect_test_commands

        return [list(cmd) for cmd in detect_test_commands(repo)][:6]
    except Exception as exc:
        return [[f"<detection-failed: {str(exc)[:120]}>"]]


def _executor_brief() -> list[dict[str, Any]]:
    """Which coding agents exist on this machine, and what they are for."""
    try:
        from app.background.executors.registry import default_registry

        return [
            {
                "id": info.id,
                "label": info.label,
                "available": info.available,
                "detail": info.detail,
                "capabilities": info.capabilities,
            }
            for info in default_registry().infos()
        ]
    except Exception as exc:
        return [{"error": str(exc)[:200]}]


def available_model_brief(limit: int = 25) -> list[str]:
    """Best-effort list of routes the local gateway can serve."""
    try:
        from app.llm.ninerouter import list_models, normalize_base_url

        base = normalize_base_url(
            os.environ.get("NINEROUTER_BASE_URL", "http://127.0.0.1:20128")
        )
        key = os.environ.get("NINEROUTER_API_KEY", "")
        inventory = list_models(base, api_key=key)
        return (
            list(inventory.get("models") or []) + list(inventory.get("combos") or [])
        )[:limit]
    except Exception:
        return []


def _read_bounded(path: Path) -> str:
    try:
        with path.open("r", errors="replace") as handle:
            return handle.read(MAX_MARKER_BYTES)
    except OSError:
        return ""
