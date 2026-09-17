"""Staged, all-or-nothing file editing for isolated worktrees.

All edits are validated and staged in memory before anything touches
the filesystem: a single failing edit rejects the whole plan. Applying
writes every staged file; restore reverts every touched file byte-for-
byte (CRLF and trailing whitespace preserved). Create and delete are
explicit edit actions; escaping the worktree root — including through
symlinks — is rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.workers.mission_evidence import now_iso

REQUIRED_FIELDS = ("target_file", "find", "replace")

EDIT_ACTIONS = ("edit", "create", "delete")


def read_text_preserve(path: Path) -> str:
    """Read without newline translation so CRLF files keep exact bytes."""
    with path.open("r", newline="") as handle:
        return handle.read()


def write_text_preserve(path: Path, text: str) -> None:
    """Write without newline translation so untouched regions stay exact."""
    with path.open("w", newline="") as handle:
        handle.write(text)


def find_relaxed_unique_match(original: str, needle: str) -> Optional[str]:
    """Find a unique multiline match ignoring per-line edge whitespace."""
    needle_lines = needle.strip().splitlines()
    if not needle_lines:
        return None

    original_lines = original.splitlines(keepends=True)
    normalized_needle = [line.strip() for line in needle_lines]
    matches = []

    for start in range(len(original_lines)):
        end = start + len(normalized_needle)
        if end > len(original_lines):
            break
        candidate = original_lines[start:end]
        if [line.strip() for line in candidate] == normalized_needle:
            matches.append("".join(candidate))

    if len(matches) == 1:
        return matches[0]
    return None


def normalize_edits(plan: Any) -> Optional[list]:
    """Accept structured multi-edit or legacy single-edit shapes."""
    if not isinstance(plan, dict):
        return None

    edits = plan.get("edits")
    if isinstance(edits, list):
        return edits

    legacy_target = plan.get("target_file")
    legacy_find = plan.get("find")
    legacy_replace = plan.get("replace")
    if legacy_target and legacy_find is not None and legacy_replace is not None:
        return [
            {
                "target_file": legacy_target,
                "find": legacy_find,
                "replace": legacy_replace,
            }
        ]
    return None


def _validate_edit(edit: Any, index: int) -> Optional[dict]:
    if not isinstance(edit, dict):
        return {
            "type": "InvalidEditPlan",
            "message": "Edit %d is not an object" % index,
        }
    action = edit.get("action", "edit")
    if action not in EDIT_ACTIONS:
        return {
            "type": "InvalidEditPlan",
            "message": "Edit %d has unknown action: %r" % (index, action),
        }
    # Tolerate plans that express creation with an omitted action:
    # find=="" on a non-existent target means "create", exactly as
    # the coder prompt documents.
    if (
        action == "edit"
        and (edit.get("find") or "") == ""
        and (edit.get("replace") or "") != ""
    ):
        action = "create"
    if action == "delete":
        if not edit.get("target_file"):
            return {
                "type": "InvalidEditPlan",
                "message": "Delete edit %d missing target_file" % index,
            }
        return None
    missing = [key for key in REQUIRED_FIELDS if key not in edit]
    if missing:
        return {
            "type": "InvalidEditPlan",
            "message": "Edit %d missing fields: %s" % (index, missing),
        }
    return None


def _resolve_target(worktree: Path, rel: str) -> Path:
    """Resolve a relative target; raise ValueError on path or symlink escape."""
    worktree_resolved = worktree.resolve()
    candidate = (worktree / rel)
    target = candidate.resolve()
    if target == worktree_resolved or worktree_resolved not in target.parents:
        raise ValueError(
            "%s escapes isolated worktree (path or symlink escape)" % rel
        )
    return target


def prepare_edits(
    worktree: Path,
    edits: list,
    evidence: Optional[list] = None,
):
    """Validate and stage every edit in memory; write nothing.

    Returns (original_contents, staged_contents, prepared_edits,
    touched_files, error); error is None on success. On any failure
    every return value except error is None and nothing is written.
    """
    evidence = [] if evidence is None else evidence
    staged_contents: dict = {}
    original_contents: dict = {}
    prepared_edits: list = []
    touched_files: list = []

    for index, edit in enumerate(edits):
        check_error = _validate_edit(edit, index)
        if check_error is not None:
            return None, None, None, None, {
                "output": {"edit_index": index},
                "error": check_error,
            }

        target_file = edit["target_file"]
        action = edit.get("action", "edit")
        # Mirror _validate_edit: an omitted action with an empty
        # find anchor is a create edit (new file / full content).
        if (
            action == "edit"
            and (edit.get("find") or "") == ""
            and (edit.get("replace") or "") != ""
        ):
            action = "create"

        try:
            target = _resolve_target(worktree, target_file)
        except ValueError as exc:
            return None, None, None, None, {
                "output": {
                    "edit_index": index,
                    "target_file": target_file,
                },
                "error": {
                    "type": "PathEscapeError",
                    "message": str(exc),
                },
            }

        if action == "create":
            if target.exists():
                return None, None, None, None, {
                    "output": {"edit_index": index, "target_file": target_file},
                    "error": {
                        "type": "TargetExists",
                        "message": "%s already exists; cannot create" % target_file,
                    },
                }
            if not edit.get("replace"):
                return None, None, None, None, {
                    "output": {"edit_index": index, "target_file": target_file},
                    "error": {
                        "type": "InvalidEditPlan",
                        "message": "Create edit %d requires non-empty replace" % index,
                    },
                }
            staged_contents[target_file] = edit["replace"]
            touched_files.append(target_file)
            prepared_edits.append(
                {
                    "edit_index": index,
                    "action": "create",
                    "target_file": target_file,
                }
            )
            continue

        if action == "delete":
            if not target.exists():
                return None, None, None, None, {
                    "output": {"edit_index": index, "target_file": target_file},
                    "error": {
                        "type": "TargetNotFound",
                        "message": "%s does not exist" % target_file,
                    },
                }
            if not target.is_file():
                return None, None, None, None, {
                    "output": {"edit_index": index, "target_file": target_file},
                    "error": {
                        "type": "TargetNotFile",
                        "message": "%s is not a file" % target_file,
                    },
                }
            original_contents[target_file] = read_text_preserve(target)
            staged_contents[target_file] = None
            touched_files.append(target_file)
            prepared_edits.append(
                {
                    "edit_index": index,
                    "action": "delete",
                    "target_file": target_file,
                }
            )
            continue

        if not target.exists():
            return None, None, None, None, {
                "output": {"edit_index": index, "target_file": target_file},
                "error": {
                    "type": "TargetNotFound",
                    "message": "%s does not exist" % target_file,
                },
            }

        if not target.is_file():
            return None, None, None, None, {
                "output": {"edit_index": index, "target_file": target_file},
                "error": {
                    "type": "TargetNotFile",
                    "message": "%s is not a file" % target_file,
                },
            }

        if target_file not in original_contents:
            original_contents[target_file] = read_text_preserve(target)

        current = staged_contents.get(
            target_file,
            original_contents[target_file],
        )

        find_text = edit["find"]
        replace_text = edit["replace"]
        actual_find_text = find_text

        if find_text not in current:
            actual_find_text = find_relaxed_unique_match(current, find_text)
            if actual_find_text is not None:
                evidence.append(
                    {
                        "type": "relaxed_match",
                        "file": target_file,
                        "requested_find": find_text,
                        "actual_find": actual_find_text,
                        "edit_index": index,
                        "timestamp": now_iso(),
                    }
                )

        if actual_find_text is None or actual_find_text not in current:
            return None, None, None, None, {
                "output": {
                    "edit_index": index,
                    "target_file": target_file,
                },
                "error": {
                    "type": "FindTextMissing",
                    "message": (
                        "Requested source text was not found as an "
                        "exact or unique relaxed match"
                    ),
                },
            }

        modified = current.replace(actual_find_text, replace_text, 1)
        staged_contents[target_file] = modified

        if target_file not in touched_files:
            touched_files.append(target_file)

        prepared_edits.append(
            {
                "edit_index": index,
                "action": "edit",
                "target_file": target_file,
                "find": find_text,
                "actual_find": actual_find_text,
                "replace": replace_text,
            }
        )

    return (
        original_contents,
        staged_contents,
        prepared_edits,
        touched_files,
        None,
    )


def apply_edits(worktree: Path, prepared_edits: list, staged_contents: dict) -> None:
    """Write every staged change; delete-and-create handled per action."""
    for prepared in prepared_edits:
        target_file = prepared["target_file"]
        action = prepared.get("action", "edit")
        target = (worktree / target_file).resolve()

        if action == "delete":
            target.unlink(missing_ok=True)
            continue

        staged = staged_contents.get(target_file)
        if staged is None:
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_preserve(target, staged)


def restore_originals(
    worktree: Path,
    original_contents: dict,
    prepared_edits: list,
    evidence: Optional[list] = None,
    retry: int = 0,
) -> None:
    """Revert every edit byte-for-byte: created files removed, deleted
    files recreated, edited files rewritten from their original text.

    Must always run after a failed validation so no half-validated
    change is left behind.
    """
    evidence = [] if evidence is None else evidence

    for prepared in prepared_edits:
        target_file = prepared["target_file"]
        action = prepared.get("action", "edit")
        target = (worktree / target_file).resolve()

        if action == "create":
            target.unlink(missing_ok=True)
            continue

        if target_file in original_contents:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_preserve(target, original_contents[target_file])

    evidence.append(
        {
            "type": "recovery",
            "action": "revert_failed_edits",
            "files": list(dict.fromkeys(p["target_file"] for p in prepared_edits)),
            "retry": retry,
            "timestamp": now_iso(),
        }
    )