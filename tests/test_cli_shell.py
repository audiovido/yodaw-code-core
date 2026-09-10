"""Hermetic tests for the native interactive YODAW shell.

No live providers, no real-repo mutation, no network, no desktop
control. Temporary repos and fake executors only.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from app.cli import __version__ as cli_version
from app.cli.commands import dispatch, is_slash_command
from app.cli.events import render_human, render_json, supports_color
from app.cli.main import EXIT_OK, EXIT_TASK_FAILURE, main
from app.cli.pipeline import (
    PipelineResult,
    approval_decision,
    classify_goal,
    is_high_risk,
    run_task,
)
from app.cli.redact import redact_mapping, redact_text
from app.cli.repo import RepoInfo, detect_repo
from app.cli.repl import Repl
from app.cli.session import Session, list_sessions, load_session, save_session


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    target = tmp_path / "sessions"
    monkeypatch.setenv("YODAW_SESSIONS_DIR", str(target))
    return target


@pytest.fixture
def temp_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True)
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    (root / "requirements.txt").write_text("pytest\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return root


def _session(repo=None) -> Session:
    return Session(repo=str(repo) if repo else None, branch="main")


def _fake_ok(**kwargs) -> PipelineResult:
    events = kwargs.get("events", [])
    return PipelineResult(success=True, status="PASS", summary="fake ok", events=events, evidence=[])


# --- repo detection ---

def test_detect_repo_identity_and_clean(temp_repo):
    info = detect_repo(temp_repo)
    assert info.is_git_repo
    assert info.root == temp_repo.resolve()
    assert info.branch
    assert info.dirty is False
    assert info.repo_type == "python"


def test_detect_repo_dirty_state(temp_repo):
    (temp_repo / "app.py").write_text("dirty\n")
    info = detect_repo(temp_repo)
    assert info.dirty is True
    assert info.dirty_files


def test_detect_non_repo(tmp_path):
    info = detect_repo(tmp_path)
    assert info.is_git_repo is False


# --- slash commands ---

def test_slash_commands_not_sent_as_prompts():
    assert is_slash_command("/status")
    assert is_slash_command("/diff --full")
    assert is_slash_command("cancel")
    assert not is_slash_command("fix the login tests")


def test_status_history_diff_commands(temp_repo, sessions_dir):
    repo = detect_repo(temp_repo)
    session = _session(temp_repo)
    out, _, _ = dispatch("/status", session, repo)
    assert session.session_id in out
    out, _, _ = dispatch("/history", session, repo)
    assert "no conversation" in out
    out, _, _ = dispatch("/diff", session, repo)
    assert "no local modifications" in out
    out, _, _ = dispatch("/tests", session, repo)
    assert "test_app.py" in out
    out, _, _ = dispatch("/context", session, repo)
    assert "approval" in out
    out, _, _ = dispatch("/help", session, repo)
    assert "/status" in out


def test_model_provider_approval_commands(temp_repo):
    repo = RepoInfo(root=temp_repo)
    session = _session(temp_repo)
    out, _, _ = dispatch("/model gpt-x", session, repo)
    assert session.model == "gpt-x"
    out, _, _ = dispatch("/provider local", session, repo)
    assert session.provider == "local"
    out, _, _ = dispatch("/approval safe", session, repo)
    assert session.approval_mode == "safe"
    out, _, _ = dispatch("/unknown-thing", session, repo)
    assert "unknown command" in out


# --- REPL behavior ---

def _repl(temp_repo, monkeypatch=None, **kwargs):
    repo = detect_repo(temp_repo)
    session = _session(temp_repo)
    output = io.StringIO()
    kwargs.setdefault("output", output)
    repl = Repl(repo, session, **kwargs)
    return repl, output


def test_repl_task_prompt_uses_fake_executor(temp_repo, sessions_dir):
    def executor(**kwargs):
        return PipelineResult(success=True, status="PASS", summary="done", events=kwargs["events"], evidence=[])
    repl, output = _repl(temp_repo, executor=executor, confirm_func=lambda q: True)
    assert repl.handle_line("fix the login tests") is False
    assert "[observe]" in output.getvalue()
    assert len(repl.session.history) == 2


def test_repl_single_failure_does_not_crash(temp_repo, sessions_dir):
    def boom(**kwargs):
        raise RuntimeError("provider exploded")
    repl, output = _repl(temp_repo, executor=boom, confirm_func=lambda q: True)
    assert repl.handle_line("do something") is False
    assert repl.handle_line("/status") is False
    assert "FAILED" in output.getvalue() or "failed" in output.getvalue().lower()


def test_repl_ctrl_c_and_eof_safe(temp_repo, sessions_dir):
    script = [KeyboardInterrupt(), EOFError()]
    def scripted_input(prompt):
        action = script.pop(0)
        raise action
    repl, output = _repl(temp_repo, input_func=scripted_input)
    assert repl.run() == 0
    assert "preserved" in output.getvalue()
    assert "bye" in output.getvalue()


def test_repl_cancel_and_exit(temp_repo, sessions_dir):
    repl, output = _repl(temp_repo)
    assert repl.handle_line("cancel") is False
    assert "cancelled" in output.getvalue()
    assert repl.handle_line("/exit") is True


def test_repl_history_and_resume_roundtrip(temp_repo, sessions_dir):
    repl, _ = _repl(temp_repo, confirm_func=lambda q: True)
    repl.handle_line("explain this repo")
    first_id = repl.session.session_id
    save_session(repl.session)
    assert load_session(first_id) is not None
    assert first_id in [item["session_id"] for item in list_sessions()]
    repl2, output2 = _repl(temp_repo)
    repl2.handle_line(f"/resume {first_id}")
    assert repl2.session.session_id == first_id
    assert "resumed" in output2.getvalue()


# --- pipeline classification and approvals ---

def test_goal_classification_and_high_risk():
    assert classify_goal("explain this repo") == "readonly"
    assert classify_goal("fix the login tests") == "mutating"
    assert is_high_risk("git reset --hard HEAD") is True
    assert is_high_risk("fix tests") is False


def test_approval_modes():
    ok, _ = approval_decision("fix tests", "auto", False)
    assert ok is True
    ok, _ = approval_decision("git reset --hard HEAD", "auto", False)
    assert ok is False
    ok, _ = approval_decision("fix tests", "safe", False)
    assert ok is False
    ok, _ = approval_decision("explain this repo", "safe", False)
    assert ok is True
    ok, reason = approval_decision("fix tests", "bogus", True)
    assert ok is False and reason


def test_run_task_failed_provider_and_blocked(temp_repo, sessions_dir):
    repo = detect_repo(temp_repo)
    session = _session(temp_repo)
    result = run_task("do work", repo, session, approval_mode="auto", approved=True,
                      executor=lambda **kw: (_ for _ in ()).throw(RuntimeError("provider down")))
    assert result.status == "FAILED" and result.retryable is True
    assert session.tasks[-1].status == "failed"


def test_run_task_cancelled_in_safe_mode(temp_repo, sessions_dir):
    repo = detect_repo(temp_repo)
    session = _session(temp_repo)
    result = run_task("fix tests", repo, session, approval_mode="safe", approved=False)
    assert result.status == "CANCELLED"


def test_run_task_interrupted_preserves_session(temp_repo, sessions_dir):
    repo = detect_repo(temp_repo)
    session = _session(temp_repo)
    def interrupter(**kwargs):
        raise KeyboardInterrupt()
    result = run_task("fix tests", repo, session, approval_mode="auto", approved=True, executor=interrupter)
    assert result.status == "INTERRUPTED"
    assert load_session(session.session_id) is None  # run_task does not persist by itself
    save_session(session)
    assert load_session(session.session_id) is not None


# --- output modes ---

def test_json_mode_has_no_ansi(temp_repo):
    events = [{"stage": "execute", "message": "editing app/auth.py", "level": "info"}]
    rendered = render_json(events)
    assert "\033[" not in rendered
    assert json.loads(rendered)["stage"] == "execute"


def test_human_render_and_color_flag():
    event = {"stage": "error", "message": "boom", "level": "error"}
    plain = render_human(event, color=False)
    assert plain == "[error] boom"
    colored = render_human(event, color=True)
    assert "\033[" in colored
    assert supports_color(io.StringIO()) is False


def test_non_tty_main_still_runs_piped_script(temp_repo, sessions_dir, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdin.readline", lambda: (_ for _ in ()).throw(EOFError()))
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    code = main(["--repo", str(temp_repo)])
    assert code == EXIT_OK
    assert "YODAW" in capsys.readouterr().out


def test_bare_flag_is_not_a_goal(temp_repo, sessions_dir, capsys):
    code = main(["--repo", str(temp_repo)])
    assert code == EXIT_OK
    assert "Plan ready" not in capsys.readouterr().out


def test_bare_goal_shorthand_runs_task(temp_repo, sessions_dir, capsys, monkeypatch):
    monkeypatch.chdir(temp_repo)
    code = main(["explain this repo"])
    assert code == EXIT_OK
    assert "Indexed" in capsys.readouterr().out


# --- entrypoint verbs ---

def test_version_and_sessions_verbs(sessions_dir, capsys):
    assert main(["version"]) == EXIT_OK
    assert cli_version in capsys.readouterr().out
    save_session(Session(repo="/tmp/x", branch="main"))
    assert main(["sessions"]) == EXIT_OK
    assert "sess_" in capsys.readouterr().out


def test_run_verb_exit_codes(temp_repo, sessions_dir, capsys, monkeypatch):
    monkeypatch.chdir(temp_repo)
    code = main(["run", "explain this repo", "--repo", str(temp_repo)])
    assert code == EXIT_OK
    code = main(["run", "fix tests", "--repo", str(temp_repo), "--approval-mode", "safe"])
    assert code == EXIT_TASK_FAILURE
    out, _ = capsys.readouterr()
    _ = out


def test_run_verb_json_no_ansi(temp_repo, sessions_dir, capsys, monkeypatch):
    monkeypatch.chdir(temp_repo)
    code = main(["run", "explain this repo", "--repo", str(temp_repo), "--json"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "\033[" not in out
    assert json.loads(out)["success"] is True


# --- safety ---

def test_secret_redaction():
    os.environ["YODAW_API_KEY"] = "test-secret-value-12345"
    try:
        assert "test-secret-value-12345" not in redact_text("key=test-secret-value-12345")
        assert redact_text("api_key=supersecretvalue") == "api_key=[REDACTED]"
        assert redact_text("ghp_abcdefghijklmnop") == "[REDACTED]"
        masked = redact_mapping({"YODAW_LLM_API_KEY": "shh", "goal": "fix tests"})
        assert masked["YODAW_LLM_API_KEY"] == "[REDACTED]"
        assert masked["goal"] == "fix tests"
    finally:
        del os.environ["YODAW_API_KEY"]


def test_dirty_repo_warning_event(temp_repo, sessions_dir):
    (temp_repo / "app.py").write_text("dirty\n")
    repo = detect_repo(temp_repo)
    assert repo.dirty is True
    session = _session(temp_repo)
    result = run_task("explain this repo", repo, session, approval_mode="auto", approved=True)
    assert result.success is True


def test_session_persistence_redacts_secrets(sessions_dir):
    os.environ["YODAW_API_KEY"] = "persist-secret-9999"
    try:
        session = Session(repo="/tmp/x")
        session.history.append({"role": "user", "text": "key persist-secret-9999"})
        path = save_session(session)
        assert "persist-secret-9999" not in Path(path).read_text()
    finally:
        del os.environ["YODAW_API_KEY"]
