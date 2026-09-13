import json
import os
import re
from pathlib import Path
from typing import Optional

from app.llm.provider import LocalLLMProvider, LLMError


REPAIR_SYSTEM_PROMPT = """
You are YODAW Coder Brain in repair mode.

A previous edit attempt failed validation.

Your job is to propose a corrective edits[] plan that fixes the
failure without repeating the same broken plan.

Rules:

1. Diagnose from the failed test output and diff why validation failed.
2. Never repeat the same broken edit.
3. Fix the failure with the smallest safe corrective edits.
4. Do not touch files outside the repository.
5. Do not output shell commands.
6. Do not change git configuration.
7. Reuse existing project code and installed dependencies.
8. Return JSON only.
9. Never wrap JSON in markdown.
10. If failure evidence is insufficient, return action="blocked".

Required JSON format:

{
  "action": "edit",
  "edits": [
    {
      "target_file": "relative/path.py",
      "find": "exact existing text",
      "replace": "replacement text"
    }
  ],
  "reason": "short explanation"
}

or:

{
  "action": "blocked",
  "reason": "why there is not enough information"
}
""".strip()

from app.learning.retrieval import (
    format_lessons,
    retrieve_relevant_learnings,
)
from app.reuse.service import build_full_reuse_intelligence


def build_lessons_context(goal: str, limit: int = 3) -> str:
    """
    Retrieve relevant prior experience for planning context.
    Retrieval is advisory: it must never corrupt a mission and
    must never modify policy or constitution files.
    """
    try:
        records = retrieve_relevant_learnings(goal, limit=limit)

        if not records:
            return ""

        lessons = format_lessons(records)

        return (
            "\nRELEVANT PRIOR EXPERIENCE (guidance only, "
            "do not blindly copy previous patches):\n\n"
            + "\n".join(lessons)
            + "\n"
        )

    except Exception:
        # Learning retrieval failure must never break planning.
        return ""


SYSTEM_PROMPT = """
You are YODAW Coder Brain.

Your job is to propose the smallest safe code edit that satisfies
the user's coding goal.

Rules:

1. Never invent files that were not shown unless creation is required.
2. Prefer modifying existing code over rewriting entire modules.
3. Do not output shell commands.
4. Do not change git configuration.
5. Do not touch files outside the repository.
6. Reuse existing project modules before creating duplicates.
7. Reuse existing installed dependencies before adding or reinventing functionality.
8. Prefer mature reusable components for common infrastructure.
9. Do not recommend writing infrastructure from scratch if the repository already contains an equivalent.
10. Make the smallest change likely to pass tests.
11. Return JSON only.
12. Never wrap JSON in markdown.
13. If information is insufficient, return action="blocked".

Required JSON format:

{
  "action": "edit",
  "edits": [
    {
      "target_file": "relative/path.py",
      "find": "exact existing text",
      "replace": "replacement text"
    }
  ],
  "reason": "short explanation"
}

Use multiple edits when the goal genuinely requires multiple files.

Legacy single-edit format is also accepted internally for compatibility.

or:

{
  "action": "blocked",
  "reason": "why there is not enough information"
}
""".strip()


def build_repo_context(
    worktree: Path,
    max_chars: int = 24000,
    max_entries: int = 200,
) -> str:
    candidates = []

    ignored_dirs = {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "build",
        "cache",
        "__pycache__",
        ".pytest_cache",
    }

    allowed_suffixes = {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".json",
        ".md",
        ".toml",
        ".yaml",
        ".yml",
        ".java",
        ".kt",
        ".swift",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
    }

    # Walk with followlinks=False: symlinked directories are never
    # traversed and symlinked files are skipped, so context can never
    # leak outside the worktree or loop on directory cycles.
    for dirpath, dirnames, filenames in os.walk(worktree, followlinks=False):
        dirnames[:] = sorted(
            d for d in dirnames if d not in ignored_dirs
        )
        for name in sorted(filenames):
            path = Path(dirpath) / name

            if path.is_symlink():
                continue

            if path.suffix.lower() not in allowed_suffixes:
                continue

            try:
                if path.stat().st_size > 120_000:
                    continue

                content = path.read_text(errors="replace")

            except OSError:
                continue

            rel = path.relative_to(worktree).as_posix()

            candidates.append(
                f"\n--- FILE: {rel} ---\n{content}\n"
            )

            if len(candidates) >= max_entries:
                return "".join(candidates)[:max_chars]

    joined = "".join(candidates)

    return joined[:max_chars]


def parse_plan(raw: str) -> dict:
    text = raw.strip()

    # Extract JSON from markdown fence if present anywhere in the response
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence_match:
        candidate = fence_match.group(1).strip()
    elif text.startswith("```"):
        candidate = text.strip("`")
        if candidate.startswith("json"):
            candidate = candidate[4:].strip()
    else:
        candidate = text

    try:
        plan = json.loads(candidate)
    except json.JSONDecodeError:
        # Fallback: attempt to find the outer-most JSON object in the text
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                plan = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMError(
                    f"Coder returned invalid JSON: {exc}"
                ) from exc
        else:
            raise LLMError(
                f"Coder returned invalid JSON: could not parse JSON object"
            )

    action = plan.get("action")

    if action not in {"edit", "blocked"}:
        raise LLMError(
            f"Unsupported coder action: {action}"
        )

    if action == "blocked":
        return plan

    # Backward compatibility with Stage 6 single-edit plans.
    if "edits" not in plan:
        required = {
            "target_file",
            "find",
            "replace",
        }

        missing = [
            key
            for key in required
            if key not in plan
        ]

        if missing:
            raise LLMError(
                f"Coder plan missing: {missing}"
            )

        plan["edits"] = [
            {
                "target_file": plan["target_file"],
                "find": plan["find"],
                "replace": plan["replace"],
            }
        ]

    edits = plan.get("edits")

    if not isinstance(edits, list) or not edits:
        raise LLMError(
            "Coder edit plan must contain a non-empty edits list"
        )

    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise LLMError(
                f"Edit {index} is not an object"
            )

        missing = [
            key
            for key in (
                "target_file",
                "find",
                "replace",
            )
            if key not in edit
        ]

        if missing:
            raise LLMError(
                f"Edit {index} missing: {missing}"
            )

    return plan


def generate_edit_plan(
    goal: str,
    worktree: Path,
    provider=None,
    lessons: str = "",
    cancel_check=None,
) -> dict:

    if cancel_check:
        cancel_check()

    provider = provider or LocalLLMProvider()

    context = build_repo_context(worktree)
    lessons = lessons or build_lessons_context(goal)
    reuse_report = build_full_reuse_intelligence(
        worktree,
        goal,
        enable_github=os.environ.get(
            "YODAW_ENABLE_GITHUB",
            "true",
        ).lower()
        in {
            "1",
            "true",
            "yes",
            "on",
        },
    )

    user_prompt = f"""
CODING GOAL:

{goal}

REUSE ANALYSIS:

{json.dumps(reuse_report, indent=2)}

REPOSITORY CONTENT:

{context}
{lessons}
Before proposing new code, check the reuse analysis.

Priority:
1. existing project code
2. existing installed dependency
3. mature reusable external component
4. only then custom implementation

Return the safest minimal JSON edit plan.
""".strip()

    raw = provider.chat(
        SYSTEM_PROMPT,
        user_prompt,
    )

    return parse_plan(raw)


def generate_repair_plan(
    goal: str,
    worktree: Path,
    previous_plan: Optional[dict],
    failure_context: dict,
    provider=None,
    lessons: str = "",
    cancel_check=None,
) -> dict:

    if cancel_check:
        cancel_check()

    provider = provider or LocalLLMProvider()

    context = build_repo_context(worktree)
    lessons = lessons or build_lessons_context(goal)

    prepare_error = failure_context.get("prepare_error")

    if prepare_error:
        prepare_block = f"""
EDIT APPLICATION FAILURE (no tests ran — the plan could not be
applied to the files above):

{json.dumps(prepare_error, indent=2)}
"""
        closing = (
            "The previous plan could not even be applied. Diagnose "
            "the application failure from the error above, match "
            "file text EXACTLY (whitespace and newlines included) "
            "using the repository content, then return the smallest "
            "safe JSON corrective plan.\n\n"
            "Do NOT repeat the same broken plan."
        )
    else:
        prepare_block = ""
        closing = (
            "The previous attempt failed validation. Diagnose the "
            "failure from the test output and the diff, then return "
            "the smallest safe JSON corrective plan.\n\n"
            "Do NOT repeat the same broken plan."
        )

    user_prompt = f"""
CODING GOAL:

{goal}

PREVIOUS PLAN THAT FAILED VALIDATION:

{json.dumps(previous_plan or {}, indent=2)}

FAILED TEST RESULTS:

{json.dumps(failure_context.get("tests", []), indent=2)}

FAILED DIFF:

{failure_context.get("diff", "")}
{prepare_block}
REPOSITORY CONTENT:

{context}
{lessons}
{closing}
""".strip()

    raw = provider.chat(
        REPAIR_SYSTEM_PROMPT,
        user_prompt,
    )
    
    return parse_plan(raw)
