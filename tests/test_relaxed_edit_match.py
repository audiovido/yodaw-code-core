from app.workers.repo_code_worker import find_relaxed_unique_match


def test_relaxed_match_handles_indentation_difference():
    original = """def add(a, b):
    return a + b
"""

    requested = """def add(a, b):
return a + b"""

    match = find_relaxed_unique_match(original, requested)

    assert match == original


def test_relaxed_match_rejects_ambiguous_matches():
    original = """    return value
    return value
"""

    assert find_relaxed_unique_match(
        original,
        "return value",
    ) is None
