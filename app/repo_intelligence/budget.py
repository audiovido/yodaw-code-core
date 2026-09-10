"""Context budgeting: token estimation and budget enforcement."""

from pathlib import Path
from typing import Union

from .models import Budget, BudgetEntry, BudgetResult

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate token count (~4 chars per token, ceiling)."""
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def estimate_file_tokens(path: Union[str, Path]) -> int:
    """Estimate tokens for a file's contents (0 on read failure)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return estimate_tokens(text)


def fit_budget(
    files: Union[list[str], dict[str, str], list[tuple[str, str]]],
    budget: Union[Budget, None] = None,
    root: Union[str, Path, None] = None,
) -> BudgetResult:
    """Fit files into a token budget (deterministic path order)."""
    budget = budget or Budget()
    items: list[tuple[str, str]] = []
    if isinstance(files, dict):
        items = sorted(files.items(), key=lambda kv: kv[0])
    elif isinstance(files, list):
        for item in files:
            if isinstance(item, tuple):
                items.append(item)
            else:
                if root is not None:
                    try:
                        text = (Path(root) / item).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        text = ""
                else:
                    text = ""
                items.append((item, text))
        items.sort(key=lambda kv: kv[0])
    entries: list[BudgetEntry] = []
    total = 0
    for path, text in items:
        tokens = estimate_tokens(text)
        truncated = tokens > budget.max_file_tokens
        if truncated:
            tokens = budget.max_file_tokens
        if total + tokens > budget.max_total_tokens:
            entries.append(BudgetEntry(path=path, tokens=tokens, truncated=truncated, included=False))
            continue
        total += tokens
        entries.append(BudgetEntry(path=path, tokens=tokens, truncated=truncated, included=True))
    included_any = any(e.included for e in entries)
    return BudgetResult(entries=entries, total_tokens=total, within_budget=included_any or not entries)
