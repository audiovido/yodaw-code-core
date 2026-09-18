import json

from app.llm.coder import build_repo_context, parse_plan
from app.workers.validation import detect_test_commands


def test_lenient_plan_accepts_raw_control_character():
    raw = (
        '{"action":"edit","target_file":"a.py","find":"x",'
        '"replace":"line1'
        + "\n"
        + 'line2"}'
    )

    result = parse_plan(raw)

    assert result["action"] == "edit"
    assert result["replace"] == "line1\nline2"


def test_plan_infers_missing_action_from_edits():
    raw = json.dumps(
        {
            "edits": [
                {
                    "target_file": "a.py",
                    "find": "x",
                    "replace": "y",
                }
            ],
            "reason": "valid edit with omitted action",
        }
    )

    result = parse_plan(raw)

    assert result["action"] == "edit"
    assert result["edits"][0]["replace"] == "y"


def test_context_skips_lockfile_and_keeps_frontend_source(tmp_path):
    (tmp_path / "package-lock.json").write_text(
        '{"blob":"' + ("x" * 30000) + '"}'
    )
    (tmp_path / "package.json").write_text(
        '{"scripts":{"build":"vite build"}}'
    )

    src = tmp_path / "src"
    src.mkdir()

    (src / "App.jsx").write_text(
        "export default function App(){return <div>REAL_UI</div>}\n"
    )
    (src / "app.css").write_text(
        ".app { display: block; }\n"
    )

    context = build_repo_context(tmp_path)

    assert "package-lock.json" not in context
    assert "FILE: src/App.jsx" in context
    assert "REAL_UI" in context
    assert "FILE: src/app.css" in context


def test_js_build_is_validation_when_no_test_script(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "vite build",
                }
            }
        )
    )
    (tmp_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}'
    )

    monkeypatch.setenv(
        "YODAW_AUTO_INSTALL_JS_DEPS",
        "true",
    )

    commands = detect_test_commands(
        tmp_path,
        tool_check=lambda name: f"/usr/bin/{name}",
    )

    assert [
        "npm",
        "ci",
        "--no-audit",
        "--no-fund",
    ] in commands

    assert ["npm", "run", "build"] in commands



def test_scoped_mutation_constraint_is_not_readonly():
    from app.cli.pipeline import is_explicit_readonly

    assert not is_explicit_readonly(
        "Implement the feature. "
        "Do not modify files outside this repository."
    )

    assert is_explicit_readonly(
        "Do not modify files or run commands."
    )
