import json
from pathlib import Path

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
from app.reuse.service import build_full_reuse_intelligence


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


def build_repo_context(worktree: Path, max_chars: int = 24000) -> str:
    candidates = []

    ignored_dirs = {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "build",
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

    for path in sorted(worktree.rglob("*")):
        if not path.is_file():
            continue

        if any(part in ignored_dirs for part in path.parts):
            continue

        if path.suffix.lower() not in allowed_suffixes:
            continue

        try:
            if path.stat().st_size > 120_000:
                continue

            content = path.read_text(errors="replace")

        except Exception:
            continue

        rel = path.relative_to(worktree)

        candidates.append(
            f"\n--- FILE: {rel} ---\n{content}\n"
        )

    joined = "".join(candidates)

    return joined[:max_chars]


def parse_plan(raw: str) -> dict:
    text = raw.strip()

    if text.startswith("```"):
        text = text.strip("`")

        if text.startswith("json"):
            text = text[4:].strip()

    try:
        plan = json.loads(text)

    except json.JSONDecodeError as exc:
        raise LLMError(
            f"Coder returned invalid JSON: {exc}"
        ) from exc

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
) -> dict:

    provider = provider or LocalLLMProvider()

    context = build_repo_context(worktree)
    reuse_report = build_full_reuse_intelligence(
        worktree,
        goal,
        enable_github=True,
    )

    user_prompt = f"""
CODING GOAL:

{goal}

REUSE ANALYSIS:

{json.dumps(reuse_report, indent=2)}

REPOSITORY CONTENT:

{context}

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
    previous_plan: dict | None,
    failure_context: dict,
    provider=None,
) -> dict:

    provider = provider or LocalLLMProvider()

    context = build_repo_context(worktree)

    user_prompt = f"""
CODING GOAL:

{goal}

PREVIOUS PLAN THAT FAILED VALIDATION:

{json.dumps(previous_plan or {}, indent=2)}

FAILED TEST RESULTS:

{json.dumps(failure_context.get("tests", []), indent=2)}

FAILED DIFF:

{failure_context.get("diff", "")}

REPOSITORY CONTENT:

{context}

The previous attempt failed validation. Diagnose the failure from
the test output and the diff, then return the smallest safe JSON
corrective plan.

Do NOT repeat the same broken plan.
""".strip()

    raw = provider.chat(
        REPAIR_SYSTEM_PROMPT,
        user_prompt,
    )
    
    return parse_plan(raw)
