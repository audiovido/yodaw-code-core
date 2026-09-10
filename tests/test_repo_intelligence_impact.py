"""Hermetic tests for impact analysis (blast radius)."""

from app.repo_intelligence.impact import blast_radius
from app.repo_intelligence.models import ImportEdge


def _e(src: str, dst: str) -> ImportEdge:
    return ImportEdge(src=src, dst=dst, raw=dst)


def test_direct_dependent():
    edges = [_e("a.py", "util.py")]
    assert blast_radius(["util.py"], edges) == {"util.py": ["a.py"]}


def test_transitive_dependent():
    edges = [_e("a.py", "b.py"), _e("b.py", "c.py")]
    assert blast_radius(["c.py"], edges) == {"c.py": ["a.py", "b.py"]}


def test_no_dependents():
    assert blast_radius(["solo.py"], [_e("a.py", "b.py")]) == {"solo.py": []}


def test_multiple_seeds():
    edges = [_e("a.py", "x.py"), _e("b.py", "y.py")]
    result = blast_radius(["x.py", "y.py"], edges)
    assert result == {"x.py": ["a.py"], "y.py": ["b.py"]}


def test_cycle_terminates():
    edges = [_e("a.py", "b.py"), _e("b.py", "a.py")]
    assert blast_radius(["a.py"], edges) == {"a.py": ["b.py"]}


def test_max_depth_limits():
    edges = [_e("a.py", "b.py"), _e("b.py", "c.py"), _e("c.py", "d.py")]
    assert blast_radius(["d.py"], edges, max_depth=1) == {"d.py": ["c.py"]}


def test_sorted_output():
    edges = [_e("b.py", "u.py"), _e("a.py", "u.py")]
    assert blast_radius(["u.py"], edges) == {"u.py": ["a.py", "b.py"]}


def test_duplicate_seeds_dedup():
    edges = [_e("a.py", "u.py")]
    assert blast_radius(["u.py", "u.py"], edges) == {"u.py": ["a.py"]}


def test_empty_edges():
    assert blast_radius(["a.py"], []) == {"a.py": []}


def test_seed_not_in_own_radius():
    edges = [_e("a.py", "a.py")]
    assert blast_radius(["a.py"], edges) == {"a.py": []}
