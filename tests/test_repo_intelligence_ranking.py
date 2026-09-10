"""Hermetic tests for file relevance ranking and task targeting."""

from app.repo_intelligence.models import Symbol
from app.repo_intelligence.ranking import rank_files, target_files, tokenize


def _sym(name: str, kind: str = "function", path: str = "a.py") -> Symbol:
    return Symbol(name=name, kind=kind, file=path, line=1)


def test_tokenize_drops_stopwords():
    tokens = tokenize("Fix the Auth handler please")
    assert "auth" in tokens and "handler" in tokens
    assert "the" not in tokens and "fix" not in tokens and "please" not in tokens


def test_path_match_scores():
    ranked = rank_files("update auth handler", ["src/auth.py", "src/other.py"])
    assert ranked and ranked[0].path == "src/auth.py"
    assert any("path match" in r for r in ranked[0].reasons)


def test_symbol_match_scores():
    syms = {"db.py": [_sym("connect_db", path="db.py")], "other.py": []}
    ranked = rank_files("fix connect_db failure", ["db.py", "other.py"], symbols=syms)
    assert ranked[0].path == "db.py"
    assert any("symbol match" in r for r in ranked[0].reasons)


def test_content_match_scores():
    contents = {"a.py": "def retry_with_backoff(): ...", "b.py": "nothing here"}
    ranked = rank_files("add retry with backoff", ["a.py", "b.py"], contents=contents)
    assert ranked[0].path == "a.py"


def test_no_match_returns_empty():
    assert rank_files("zebra quasar", ["a.py", "b.py"]) == []


def test_stable_tie_break():
    ranked = rank_files("auth", ["b/auth.py", "a/auth.py"])
    assert [r.path for r in ranked] == ["a/auth.py", "b/auth.py"]


def test_top_n_limits():
    ranked = rank_files("auth", ["a/auth.py", "b/auth.py", "c/auth.py"], top_n=2)
    assert len(ranked) == 2


def test_target_files_dict_input():
    files = {"auth.py": "handle auth login", "other.py": "unrelated"}
    ranked = target_files("fix auth login", files, top_n=5)
    assert ranked[0].path == "auth.py"


def test_target_files_default_top_n():
    files = [f"m{i}/auth.py" for i in range(15)]
    assert len(target_files("auth", files)) == 10


def test_reasons_present():
    ranked = rank_files("auth", ["auth.py"])
    assert ranked[0].reasons
    assert ranked[0].score > 0
