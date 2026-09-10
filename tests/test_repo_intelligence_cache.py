"""Hermetic tests for the mtime+size repo cache."""

import time

from app.repo_intelligence.cache import RepoCache


def _write(base, rel: str, content: str = "x = 1\n"):
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_put_get_hit(tmp_path):
    _write(tmp_path, "a.py")
    cache = RepoCache()
    cache.put(tmp_path, "evidence", ["a.py"], {"ok": True})
    assert cache.get(tmp_path, "evidence", ["a.py"]) == {"ok": True}


def test_get_miss_empty():
    cache = RepoCache()
    assert cache.get("/nonexistent", "evidence", ["a.py"]) is None


def test_invalidate_on_mtime_change(tmp_path):
    p = _write(tmp_path, "a.py", "v1\n")
    cache = RepoCache()
    cache.put(tmp_path, "evidence", ["a.py"], "old")
    new_mtime = p.stat().st_mtime + 5
    import os
    os.utime(p, (new_mtime, new_mtime))
    assert cache.get(tmp_path, "evidence", ["a.py"]) is None


def test_invalidate_on_size_change(tmp_path):
    _write(tmp_path, "a.py", "v1\n")
    cache = RepoCache()
    cache.put(tmp_path, "evidence", ["a.py"], "old")
    time.sleep(0.01)
    _write(tmp_path, "a.py", "v1 with more content\n")
    assert cache.get(tmp_path, "evidence", ["a.py"]) is None


def test_invalidate_by_name(tmp_path):
    _write(tmp_path, "a.py")
    cache = RepoCache()
    cache.put(tmp_path, "a", ["a.py"], 1)
    cache.put(tmp_path, "b", ["a.py"], 2)
    assert cache.invalidate(tmp_path, "a") == 1
    assert cache.get(tmp_path, "a", ["a.py"]) is None
    assert cache.get(tmp_path, "b", ["a.py"]) == 2


def test_invalidate_by_root(tmp_path, tmp_path_factory=None):
    import tempfile
    from pathlib import Path
    other = Path(tempfile.mkdtemp())
    _write(tmp_path, "a.py")
    (other / "a.py").write_text("x=1\n", encoding="utf-8")
    cache = RepoCache()
    cache.put(tmp_path, "e", ["a.py"], 1)
    cache.put(other, "e", ["a.py"], 2)
    assert cache.invalidate(tmp_path) == 1
    assert len(cache) == 1


def test_invalidate_all(tmp_path):
    _write(tmp_path, "a.py")
    cache = RepoCache()
    cache.put(tmp_path, "a", ["a.py"], 1)
    cache.put(tmp_path, "b", ["a.py"], 2)
    assert cache.invalidate() == 2
    assert len(cache) == 0


def test_put_overwrites(tmp_path):
    _write(tmp_path, "a.py")
    cache = RepoCache()
    cache.put(tmp_path, "e", ["a.py"], 1)
    cache.put(tmp_path, "e", ["a.py"], 2)
    assert cache.get(tmp_path, "e", ["a.py"]) == 2
    assert len(cache) == 1


def test_missing_file_fingerprint(tmp_path):
    cache = RepoCache()
    cache.put(tmp_path, "e", ["ghost.py"], 1)
    assert cache.get(tmp_path, "e", ["ghost.py"]) == 1


def test_different_paths_different_keys(tmp_path):
    _write(tmp_path, "a.py")
    _write(tmp_path, "b.py")
    cache = RepoCache()
    cache.put(tmp_path, "e", ["a.py"], "A")
    assert cache.get(tmp_path, "e", ["b.py"]) is None
