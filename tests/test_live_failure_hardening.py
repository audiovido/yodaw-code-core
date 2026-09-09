"""
Stage 7.7 live-mission hardening regression tests.

Root cause of the live FAIL:
  - Ollama ran qwen2.5-coder:7b CPU-only; the initial plan call
    exceeded the hard 1200s httpx timeout.
  - The attempt-0 LLMError fell through to the worker's outer
    catch-all, which returned WITHOUT worktree cleanup evidence
    (silent lifecycle abandonment).

These tests pin the corrected failure behavior hermetically:
no network, no model, no timeouts.
"""
import os
import subprocess
from pathlib import Path

import app.workers.repo_code_worker as worker_module
from app.llm.provider import LLMError, llm_keep_alive, llm_timeout_seconds
from app.workers.repo_code_worker import RepoCodeWorker


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def init_repo(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)

    git(repo, "init")
    git(repo, "config", "user.email", "yodaw@test.local")
    git(repo, "config", "user.name", "YODAW Test")

    (repo / "greet.py").write_text(
        "def greet(name):\n    return 'Hello ' + name\n"
    )
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = .\n")

    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")

    return git(repo, "rev-parse", "HEAD")


def cleanup_events(result):
    return [
        item
        for item in result["evidence"]
        if isinstance(item, dict)
        and item.get("type") == "worktree_cleanup"
    ]


def test_initial_plan_llm_error_records_cleanup_evidence(tmp_path, monkeypatch):
    repo = tmp_path / "repo_llm_timeout"
    baseline = init_repo(repo)

    def failing_generate_edit_plan(*args, **kwargs):
        raise LLMError("Ollama request failed: timed out")

    monkeypatch.setattr(
        worker_module,
        "generate_edit_plan",
        failing_generate_edit_plan,
    )

    result = RepoCodeWorker().execute(
        "Refactor greet(name) to use an f-string without changing behavior.",
        {"repo_path": str(repo)},
    )

    # Structured, retryable LLM failure surfaced through the
    # handled path, not the silent catch-all.
    assert result["success"] is False
    assert result["error"]["type"] == "LLMError"
    assert "timed out" in result["error"]["message"]
    assert result["error"]["attempt"] == 0
    assert result["retryable"] is True

    output = result["output"]
    assert output["tests_passed"] is False
    assert output["retries"] == 0
    assert output["attempts"] == 1

    # Lifecycle was recorded, never silent.
    events = cleanup_events(result)
    assert events, "worktree_cleanup evidence missing on LLM failure"
    assert events[0]["action"] == "kept_failed_for_debugging"

    # Worktree kept for debugging: restored, clean, zero commits.
    worktree = Path(output["worktree"])
    assert worktree.exists()
    assert worker_module.worktree_is_clean(worktree) is True
    assert git(worktree, "rev-list", "--count", "HEAD") == "1"
    assert (worktree / "greet.py").read_text() == (
        "def greet(name):\n    return 'Hello ' + name\n"
    )

    # Source repository never mutated.
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_unhandled_worker_failure_still_records_cleanup(tmp_path):
    repo = tmp_path / "repo_unhandled"
    baseline = init_repo(repo)

    real_detect = worker_module.detect_test_commands

    def exploding_detect(worktree):
        raise RuntimeError("boom: unhandled worker failure")

    worker_module.detect_test_commands = exploding_detect
    try:
        result = RepoCodeWorker().execute(
            "Any goal.",
            {
                "repo_path": str(repo),
                "edits": [
                    {
                        "target_file": "greet.py",
                        "find": "return 'Hello ' + name",
                        "replace": "return 'Hello ' + name",
                    }
                ],
            },
        )
    finally:
        worker_module.detect_test_commands = real_detect

    assert result["success"] is False
    assert result["error"]["type"] == "RuntimeError"
    assert "boom" in result["error"]["message"]

    # Outer catch-all must now record the worktree lifecycle.
    events = cleanup_events(result)
    assert events, "outer catch-all must record worktree_cleanup"
    assert events[0]["action"] == "kept_failed_for_debugging"

    worktree = Path(result["output"]["worktree"])
    assert worktree.exists()
    assert worker_module.worktree_is_clean(worktree) is True

    # Source repository never mutated.
    assert git(repo, "rev-parse", "HEAD") == baseline


def test_llm_timeout_and_keep_alive_are_configurable(monkeypatch):
    monkeypatch.delenv("YODAW_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("YODAW_LLM_KEEP_ALIVE", raising=False)

    assert llm_timeout_seconds() == 1200
    assert llm_keep_alive() == "5m"

    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "3600")
    monkeypatch.setenv("YODAW_LLM_KEEP_ALIVE", "60m")

    assert llm_timeout_seconds() == 3600
    assert llm_keep_alive() == "60m"

    monkeypatch.setenv("YODAW_LLM_TIMEOUT_SECONDS", "not-a-number")
    assert llm_timeout_seconds() == 1200
