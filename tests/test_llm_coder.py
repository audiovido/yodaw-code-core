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

def test_repo_context_ignores_vendor_and_build_trees():
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "main.py").write_text("x = 1\n")
        (root / "keep.py").write_text("y = 2\n")
        for ignored in (".git", "node_modules", "dist", "build", "cache"):
            (root / ignored).mkdir()
            (root / ignored / "blob.py").write_text("secret payload\n")

        context = build_repo_context(root)

        assert "main.py" in context
        assert "keep.py" in context
        assert "blob.py" not in context
        assert "secret payload" not in context
        for ignored in (".git", "node_modules", "dist", "build", "cache"):
            assert ignored not in context

def test_repo_context_entry_budget():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for index in range(30):
            (root / ("file_%02d.py" % index)).write_text(
                "value = %d\n" % index
            )

        bounded = build_repo_context(root, max_entries=3)
        bundled = build_repo_context(root)

        assert bounded.count("--- FILE:") == 3
        assert bundled.count("--- FILE:") == 30
        assert "file_29.py" in bundled

def test_repo_context_skips_symlinks():
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "main.py").write_text("x = 1\n")
        outside = root.parent / "outside_secret_yodaw_test.py"
        outside.write_text("SECRET = 'dont-leak'\n")
        _os.symlink(outside, root / "leak.py")
        _os.symlink(root, root / "self_loop")
        _os.symlink(root, outside.parent / "outside_loop_yodaw_test")

        try:
            context = build_repo_context(root)

            assert "main.py" in context
            assert "leak.py" not in context
            assert "SECRET" not in context
            assert "self_loop" not in context
        finally:
            for stray in (outside, outside.parent / "outside_loop_yodaw_test"):
                if stray.exists() or stray.is_symlink():
                    stray.unlink(missing_ok=True)

def test_repo_context_format_preserved():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "main.py").write_text("x = 1\n")
        (root / "z.py").write_text("z = 9\n")

        context = build_repo_context(root)

        assert context.startswith("\n--- FILE: ")
        assert "\n--- FILE: main.py ---\n" in context
        assert "\n--- FILE: z.py ---\n" in context
        assert context.endswith("\n")
        # Deterministic order regardless of call count.
        assert context == build_repo_context(root)


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
