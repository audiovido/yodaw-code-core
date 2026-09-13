"""Focused tests for the staged edit engine: escape rejection, create,
delete, all-or-nothing multi-edit, byte-exact restore, and constraints."""

import os
from pathlib import Path

from app.workers.edit_engine import (
    apply_edits,
    find_relaxed_unique_match,
    normalize_edits,
    prepare_edits,
    read_text_preserve,
    restore_originals,
    write_text_preserve,
)


def _no_error(result):
    return result[4]


def test_path_escape_rejected(tmp_path):
    edits = [
        {
            "target_file": "../../etc/passwd",
            "find": "x",
            "replace": "y",
        }
    ]
    original, staged, prepared, touched, error = prepare_edits(tmp_path, edits)
    assert error is not None
    assert error["error"]["type"] == "PathEscapeError"
    assert original is None and staged is None and prepared is None and touched is None


def test_absolute_path_escape_rejected(tmp_path):
    edits = [{"target_file": "/etc/passwd", "find": "x", "replace": "y"}]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "PathEscapeError"


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path.parent / "outside_secret_yodaw.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    try:
        edits = [
            {
                "target_file": "link.txt",
                "find": "secret",
                "replace": "leaked",
            }
        ]
        _, _, _, _, error = prepare_edits(tmp_path, edits, [])
        assert error is not None
        assert error["error"]["type"] == "PathEscapeError"
    finally:
        outside.unlink(missing_ok=True)


def test_create(tmp_path):
    edits = [
        {
            "action": "create",
            "target_file": "new_file.py",
            "find": "",
            "replace": "value = 1\n",
        }
    ]
    original, staged, prepared, touched, error = prepare_edits(tmp_path, edits)
    assert error is None
    assert "new_file.py" in staged
    assert not (tmp_path / "new_file.py").exists(), "prepare must not write"

    apply_edits(tmp_path, prepared, staged)
    assert (tmp_path / "new_file.py").read_text(encoding="utf-8") == "value = 1\n"

    restored_evidence = []
    restore_originals(tmp_path, original, prepared, restored_evidence, retry=0)
    assert not (tmp_path / "new_file.py").exists(), "created file must be removed on restore"


def test_create_existing_rejected(tmp_path):
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    edits = [
        {
            "action": "create",
            "target_file": "keep.py",
            "find": "",
            "replace": "y = 2\n",
        }
    ]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "TargetExists"


def test_delete(tmp_path):
    (tmp_path / "old.py").write_text("old content\n", encoding="utf-8")
    edits = [{"action": "delete", "target_file": "old.py", "find": "", "replace": ""}]
    original, staged, prepared, touched, error = prepare_edits(tmp_path, edits)
    assert error is None
    assert staged["old.py"] is None

    apply_edits(tmp_path, prepared, staged)
    assert not (tmp_path / "old.py").exists()

    restore_originals(tmp_path, original, prepared, [], retry=0)
    assert (tmp_path / "old.py").read_text(encoding="utf-8") == "old content\n"


def test_delete_missing_rejected(tmp_path):
    edits = [{"action": "delete", "target_file": "ghost.py", "find": "", "replace": ""}]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "TargetNotFound"


def test_all_or_nothing_multi_edit(tmp_path):
    (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("beta\n", encoding="utf-8")

    edits = [
        {"target_file": "a.py", "find": "alpha", "replace": "ALPHA"},
        {"target_file": "b.py", "find": "missing_text_zzz", "replace": "BETA"},
    ]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error is not None
    assert error["error"]["type"] == "FindTextMissing"
    # Nothing was written: all-or-nothing.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "alpha\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "beta\n"


def test_byte_exact_restore(tmp_path):
    path = tmp_path / "crlf.py"
    original_bytes = b"line one\r\nline two  \r\n"
    path.write_bytes(original_bytes)

    text = read_text_preserve(path)
    assert text == "line one\r\nline two  \r\n"

    edits = [
        {"target_file": "crlf.py", "find": "line two", "replace": "line TWO"}
    ]
    original, staged, prepared, touched, error = prepare_edits(tmp_path, edits)
    assert error is None

    apply_edits(tmp_path, prepared, staged)
    assert b"\r\n" in path.read_bytes()

    restore_originals(tmp_path, original, prepared, [], retry=1)
    assert path.read_bytes() == original_bytes, "restore must be byte-exact"


def test_relaxed_unique_match():
    original = (
        "def f():\r\n"
        "    return 1\r\n"
    )
    match = find_relaxed_unique_match(original, "def f():\nreturn 1")
    assert match == "def f():\r\n    return 1\r\n"


def test_relaxed_match_is_unique_required():
    original = "a\nb\na\nb\n"
    assert find_relaxed_unique_match(original, "a\nb") is None


def test_constraints_invalid_edit_object(tmp_path):
    _, _, _, _, error = prepare_edits(tmp_path, ["not-a-dict"])
    assert error["error"]["type"] == "InvalidEditPlan"
    assert "not an object" in error["error"]["message"]


def test_constraints_missing_fields(tmp_path):
    edits = [{"target_file": "a.py", "find": "x"}]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "InvalidEditPlan"
    assert "missing fields" in error["error"]["message"]


def test_constraints_unknown_action(tmp_path):
    edits = [{"action": "explode", "target_file": "a.py", "find": "", "replace": ""}]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "InvalidEditPlan"


def test_constraints_target_not_found(tmp_path):
    edits = [{"target_file": "ghost.py", "find": "x", "replace": "y"}]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "TargetNotFound"


def test_constraints_target_not_file(tmp_path):
    (tmp_path / "adir").mkdir()
    edits = [{"target_file": "adir", "find": "x", "replace": "y"}]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "TargetNotFile"


def test_constraints_find_missing(tmp_path):
    (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
    edits = [{"target_file": "a.py", "find": "missing", "replace": "y"}]
    _, _, _, _, error = prepare_edits(tmp_path, edits)
    assert error["error"]["type"] == "FindTextMissing"


def test_edit_evidence_for_relaxed_match(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    evidence = []
    edits = [{"target_file": "a.py", "find": "def f():\nreturn 1", "replace": "def g():\n    return 2"}]
    _, _, _, _, error = prepare_edits(tmp_path, edits, evidence)
    assert error is None
    assert any(event["type"] == "relaxed_match" for event in evidence)


def test_normalize_edits_shapes():
    assert normalize_edits(
        {"edits": [{"target_file": "a.py", "find": "x", "replace": "y"}]}
    ) == [{"target_file": "a.py", "find": "x", "replace": "y"}]
    legacy = normalize_edits({"target_file": "a.py", "find": "x", "replace": "y"})
    assert legacy == [{"target_file": "a.py", "find": "x", "replace": "y"}]
    assert normalize_edits({"nope": 1}) is None
    assert normalize_edits("junk") is None


def test_multi_edit_apply_all(tmp_path):
    (tmp_path / "a.py").write_text("A\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("B\n", encoding="utf-8")
    edits = [
        {"target_file": "a.py", "find": "A", "replace": "AA"},
        {"target_file": "b.py", "find": "B", "replace": "BB"},
    ]
    original, staged, prepared, touched, error = prepare_edits(tmp_path, edits)
    assert error is None and len(touched) == 2
    apply_edits(tmp_path, prepared, staged)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "AA\n"
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "BB\n"