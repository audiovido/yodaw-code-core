import tempfile
from pathlib import Path

from app.llm.coder import (
    build_repo_context,
    generate_edit_plan,
    parse_plan,
)


class FakeProvider:
    def chat(self, system, user):
        return """
        {
          "action": "edit",
          "target_file": "app.py",
          "find": "return 1",
          "replace": "return 2",
          "reason": "implement requested behavior"
        }
        """


def test_parse_edit_plan():
    result = parse_plan(
        '''
        {
          "action": "edit",
          "target_file": "a.py",
          "find": "x = 1",
          "replace": "x = 2",
          "reason": "test"
        }
        '''
    )

    assert result["action"] == "edit"
    assert result["target_file"] == "a.py"


def test_repo_context_contains_code():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "app.py").write_text(
            "def value():\\n"
            "    return 1\\n"
        )

        context = build_repo_context(root)

        assert "FILE: app.py" in context
        assert "return 1" in context


def test_generate_plan_with_fake_provider():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "app.py").write_text(
            "def value():\\n"
            "    return 1\\n"
        )

        plan = generate_edit_plan(
            "Change value to two",
            root,
            provider=FakeProvider(),
        )

        assert plan["action"] == "edit"
        assert plan["target_file"] == "app.py"
        assert plan["replace"] == "return 2"


def test_parse_multi_edit_plan():
    result = parse_plan(
        '''
        {
          "action": "edit",
          "edits": [
            {
              "target_file": "service.py",
              "find": "return 1",
              "replace": "return helper()"
            },
            {
              "target_file": "helpers.py",
              "find": "pass",
              "replace": "return 1"
            }
          ],
          "reason": "split implementation"
        }
        '''
    )

    assert result["action"] == "edit"
    assert len(result["edits"]) == 2
    assert result["edits"][0]["target_file"] == "service.py"


def test_legacy_single_edit_is_normalized():
    result = parse_plan(
        '''
        {
          "action": "edit",
          "target_file": "app.py",
          "find": "return 1",
          "replace": "return 2",
          "reason": "legacy"
        }
        '''
    )

    assert len(result["edits"]) == 1
    assert result["edits"][0]["target_file"] == "app.py"
