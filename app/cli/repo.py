"""Repository detection and working-context summary for the CLI."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepoInfo:
    """Snapshot of the repository the CLI was launched in."""
    root: Path | None = None
    is_git_repo: bool = False
    branch: str | None = None
    dirty: bool = False
    dirty_files: list[str] = field(default_factory=list)
    repo_type: str = "generic"
    languages: dict[str, int] = field(default_factory=dict)
    file_count: int = 0
    total_size: int = 0
    truncated: bool = False
    error: str | None = None


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
    )


def detect_repo(start: Path | str = ".") -> RepoInfo:
    """Detect git identity, dirty state, and project shape for start."""
    info = RepoInfo()
    cwd = Path(start).expanduser().resolve()
    start_dir = cwd if cwd.is_dir() else cwd.parent

    top_level: Path | None = None
    try:
        proc = _git(["rev-parse", "--show-toplevel"], start_dir)
        if proc.returncode != 0:
            info.root = start_dir
            _fill_project_shape(info, start_dir)
            return info
        top_level = Path(proc.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError) as exc:
        info.root = start_dir
        info.error = str(exc)
        return info

    info.root = top_level
    info.is_git_repo = True

    try:
        branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], top_level)
        if branch.returncode == 0:
            info.branch = branch.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        info.branch = None

    try:
        status = _git(["status", "--short"], top_level)
        if status.returncode == 0:
            files = [line for line in status.stdout.splitlines() if line.strip()]
            info.dirty = bool(files)
            info.dirty_files = files[:50]
        else:
            info.error = (status.stderr or status.stdout).strip() or "git status failed"
    except (OSError, subprocess.SubprocessError) as exc:
        info.error = str(exc)

    _fill_project_shape(info, top_level)
    return info


def _fill_project_shape(info: RepoInfo, root: Path) -> None:
    """Fill repo_type/languages from Repo Intelligence without raising."""
    try:
        from app.repo_intelligence.engine import RepoIntelligence
        evidence = RepoIntelligence().analyze(root)
        info.repo_type = evidence.repo_type.value
        info.languages = dict(evidence.language_breakdown)
        info.file_count = evidence.file_count
        info.total_size = evidence.total_size
        info.truncated = evidence.truncated
    except Exception as exc:
        info.error = info.error or str(exc)


def startup_lines(info: RepoInfo) -> list[str]:
    """Concise startup summary shown once per REPL launch."""
    if not info.is_git_repo:
        return [f"repo: {info.root} (not a git repository)"]
    state = "dirty" if info.dirty else "clean"
    lines = [
        f"repo: {info.root}",
        f"branch: {info.branch or 'unknown'} ({state})",
    ]
    if info.languages:
        top = ", ".join(f"{lang} {count}" for lang, count in sorted(info.languages.items())[:5])
        lines.append(f"project: {info.repo_type} | {top}")
    if info.dirty:
        lines.append("YODAW will not touch your uncommitted changes without approval.")
    return lines
