"""Keyword/symbol/path relevance ranking for task-to-file targeting."""

import re
from pathlib import PurePosixPath
from typing import Optional, Union

from .models import RankedFile, Symbol

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

W_KEYWORD = 2.0
W_SYMBOL = 3.0
W_PATH = 1.5

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "to", "in", "on", "of", "for", "with",
    "fix", "add", "update", "change", "make", "use", "using", "file",
    "please", "should", "need", "needs", "that", "this", "is", "are",
})


def tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens, dropping stopwords."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        for chunk in raw.split("_"):
            for part in re.split(r"(?<=[a-z])(?=[A-Z])", chunk):
                low = part.lower()
                if low and low not in _STOPWORDS:
                    out.append(low)
    return out


def _symbol_tokens(names: set[str]) -> set[str]:
    """Expand symbol names into matchable tokens (full + sub-parts)."""
    expanded: set[str] = set()
    for name in names:
        low = name.lower()
        expanded.add(low)
        for chunk in low.split("_"):
            for part in re.split(r"(?<=[a-z])(?=[A-Z])", chunk):
                bits = re.split(r"[^a-z0-9]+", part)
                for bit in bits:
                    if bit:
                        expanded.add(bit)
    return expanded


def _split_path(path: str) -> set[str]:
    """Split a path into lowercase name tokens."""
    stem = PurePosixPath(path).stem
    parts = re.split(r"[^A-Za-z0-9]+", path.lower()) + re.split(r"[^A-Za-z0-9]+", stem.lower())
    parts += re.split(r"(?<=[a-z])(?=[A-Z])", stem)
    return {p.lower() for p in parts if p}


def score_file(
    task_tokens: set[str],
    symbol_names: set[str],
    path: str,
    keywords: Optional[set[str]] = None,
) -> tuple[float, list[str]]:
    """Score one file; returns (score, reasons)."""
    reasons: list[str] = []
    score = 0.0
    text_tokens = _split_path(path)
    kw_hit = task_tokens & text_tokens
    if kw_hit:
        score += W_KEYWORD * len(kw_hit)
        reasons.append("path match: " + ",".join(sorted(kw_hit)))
    sym_lower = _symbol_tokens(symbol_names)
    sym_hit = task_tokens & sym_lower
    if sym_hit:
        score += W_SYMBOL * len(sym_hit)
        reasons.append("symbol match: " + ",".join(sorted(sym_hit)))
    if keywords:
        overlap = keywords & text_tokens
        if overlap:
            score += W_KEYWORD * len(overlap)
            reasons.append("keyword overlap: " + ",".join(sorted(overlap)))
    return score, reasons


def rank_files(
    task: str,
    files: list[str],
    symbols: Optional[dict[str, list[Symbol]]] = None,
    contents: Optional[dict[str, str]] = None,
    top_n: Optional[int] = None,
) -> list[RankedFile]:
    """Rank files by relevance to task text (deterministic ordering)."""
    task_tokens = set(tokenize(task))
    sym_by_file: dict[str, set[str]] = {}
    for path, syms in (symbols or {}).items():
        sym_by_file[path] = {s.name for s in syms}
    ranked: list[RankedFile] = []
    for path in files:
        names = sym_by_file.get(path, set())
        content_kw: set[str] = set()
        if contents and path in contents:
            content_kw = set(tokenize(contents[path])) & task_tokens
        kw_score = W_KEYWORD * len(content_kw) if content_kw else 0.0
        s, reasons = score_file(task_tokens, names, path)
        total = s + kw_score
        if content_kw:
            reasons.append("content match: " + ",".join(sorted(content_kw)))
        if total > 0:
            ranked.append(RankedFile(path=path, score=total, reasons=reasons))
    ranked.sort(key=lambda r: (-r.score, r.path))
    if top_n is not None:
        ranked = ranked[:top_n]
    return ranked


def target_files(
    task: str,
    files: Union[list[str], dict[str, str]],
    symbols: Optional[dict[str, list[Symbol]]] = None,
    top_n: int = 10,
) -> list[RankedFile]:
    """Return top-N ranked files with reasons for a task description."""
    if isinstance(files, dict):
        paths = sorted(files.keys())
        contents = files
    else:
        paths = sorted(files)
        contents = None
    return rank_files(task, paths, symbols=symbols, contents=contents, top_n=top_n)
