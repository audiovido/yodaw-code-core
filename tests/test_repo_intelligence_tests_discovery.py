"""Hermetic tests for test discovery and source<->test mapping."""

from app.repo_intelligence.tests_discovery import discover_tests, is_test_file, map_source_to_test


def test_python_test_prefix():
    assert is_test_file("tests/test_auth.py") is True
    assert is_test_file("test_auth.py") is True
    assert is_test_file("src/auth.py") is False


def test_python_test_suffix():
    assert is_test_file("auth_test.py") is True


def test_js_test_suffixes():
    assert is_test_file("src/auth.test.js") is True
    assert is_test_file("src/auth.spec.ts") is True
    assert is_test_file("src/auth.js") is False


def test_go_test_suffix():
    assert is_test_file("server_test.go") is True
    assert is_test_file("server.go") is False


def test_rust_tests_dir():
    assert is_test_file("tests/integration.rs") is True
    assert is_test_file("src/main.rs") is False


def test_test_dir_convention():
    assert is_test_file("tests/auth.py") is True
    assert is_test_file("__tests__/auth.js") is True


def test_discover_sorted():
    files = ["b/test_b.py", "a/test_a.py", "src/x.py"]
    assert discover_tests(files) == ["a/test_a.py", "b/test_b.py"]


def test_map_python_source_to_test():
    files = ["src/auth.py", "tests/test_auth.py", "src/other.py"]
    result = map_source_to_test(files)
    assert result["src/auth.py"] == "tests/test_auth.py"
    assert result["src/other.py"] is None


def test_map_js_source_to_test():
    files = ["src/auth.js", "src/auth.test.js"]
    assert map_source_to_test(files)["src/auth.js"] == "src/auth.test.js"


def test_map_go_source_to_test():
    files = ["server.go", "server_test.go"]
    assert map_source_to_test(files)["server.go"] == "server_test.go"


def test_map_skips_test_files():
    files = ["tests/test_a.py", "src/a.py"]
    result = map_source_to_test(files)
    assert "tests/test_a.py" not in result


def test_map_skips_non_code():
    files = ["README.md", "src/a.py"]
    result = map_source_to_test(files)
    assert "README.md" not in result
