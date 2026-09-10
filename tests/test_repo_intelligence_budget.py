"""Hermetic tests for context budgeting."""

from pathlib import Path

from app.repo_intelligence.budget import estimate_file_tokens, estimate_tokens, fit_budget
from app.repo_intelligence.models import Budget


def test_estimate_tokens_ceil():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("a" * 8) == 2


def test_estimate_file_tokens(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("abcd", encoding="utf-8")
    assert estimate_file_tokens(p) == 1
    assert estimate_file_tokens(tmp_path / "missing.txt") == 0


def test_fit_budget_dict_includes_all():
    result = fit_budget({"a.py": "ab", "b.py": "cdef"})
    assert result.total_tokens == 2
    assert result.within_budget is True
    assert all(e.included for e in result.entries)
    assert [e.path for e in result.entries] == ["a.py", "b.py"]


def test_file_cap_truncates():
    result = fit_budget({"a.py": "x" * 100}, Budget(max_total_tokens=100, max_file_tokens=5))
    assert result.entries[0].tokens == 5
    assert result.entries[0].truncated is True
    assert result.entries[0].included is True


def test_total_budget_excludes_overflow():
    files = {"a.py": "x" * 40, "b.py": "y" * 40}  # 10 tokens each
    result = fit_budget(files, Budget(max_total_tokens=10, max_file_tokens=100))
    assert result.entries[0].included is True
    assert result.entries[1].included is False
    assert result.total_tokens == 10


def test_sorted_deterministic():
    result = fit_budget({"b.py": "ab", "a.py": "ab"})
    assert [e.path for e in result.entries] == ["a.py", "b.py"]


def test_tuple_list_input():
    result = fit_budget([("b.py", "ab"), ("a.py", "ab")])
    assert [e.path for e in result.entries] == ["a.py", "b.py"]
    assert result.total_tokens == 2


def test_str_list_with_root(tmp_path):
    (tmp_path / "a.py").write_text("abcd", encoding="utf-8")
    result = fit_budget(["a.py"], root=tmp_path)
    assert result.entries[0].tokens == 1


def test_str_list_missing_root(tmp_path):
    result = fit_budget(["nope.py"], root=tmp_path)
    assert result.entries[0].tokens == 0
    assert result.entries[0].included is True


def test_to_dict_serializable():
    import json
    result = fit_budget({"a.py": "ab"})
    json.dumps(result.to_dict())


def test_empty_within_budget():
    result = fit_budget({})
    assert result.within_budget is True
    assert result.total_tokens == 0
