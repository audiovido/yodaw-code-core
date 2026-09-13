import tempfile
from pathlib import Path
import subprocess
from app.workers.code_worker import CodeWorker


def test_real_code_worker_end_to_end():
    # Create a temporary git repository for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True)
        
        # Create a test file with actual test content
        test_file = repo_path / "test_calculator.py"
        original_content = '''def add(a, b):
    return a + b

def subtract(a, b):
    return a - b
'''
        test_file.write_text(original_content)
        
        # Create a proper test file that pytest can detect and run
        test_test_file = repo_path / "test_test_calculator.py"
        test_test_content = '''
import sys
sys.path.insert(0, '.')

from test_calculator import add, subtract

def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0
    assert add(0, 0) == 0

def test_subtract():
    assert subtract(5, 3) == 2
    assert subtract(0, 4) == -4
    assert subtract(3, 3) == 0
'''
        test_test_file.write_text(test_test_content)
        
        # Create pytest configuration to ensure detection
        pytest_ini = repo_path / "pytest.ini"
        pytest_ini.write_text("""[pytest]
testpaths = .
python_files = test_*.py
python_functions = test_*
""")
        
        # Initial commit
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit with test"], cwd=repo_path, check=True, capture_output=True)
        
        worker = CodeWorker()
        
        # Define the new content: add a multiply function to the calculator
        new_content = original_content + '''\n\ndef multiply(a, b):
    return a * b

def divide(a, b):
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b
'''
        
        # Execute with repository context and explicit edits
        result = worker.execute(
            "Add multiply and divide functions to calculator",
            metadata={
                "repo_path": str(repo_path),
                "edits": [{
                    "target_file": "test_calculator.py",
                    "find": original_content,
                    "replace": new_content,
                }]
            }
        )
        
        # Verify the result contains real evidence from actual test execution
        assert result["success"] is True
        # These should now be real values from the RepoCodeWorker execution
        assert "tests_passed" in result["output"]
        assert result["output"]["tests_passed"] is True  # Actual test execution should pass
        assert "commit_sha" in result["output"]
        assert len(result["output"]["commit_sha"]) == 40  # SHA-1 length
        assert "working_tree_clean" in result["output"]
        assert result["output"]["working_tree_clean"] is True
        assert len(result["evidence"]) >= 5  # We should have several evidence items
        
        # Additional verification: check that we have actual evidence of
        # test execution (not a fabricated result).
        assert any(
            item.get("type") == "validation"
            or "test" in str(item).lower()
            for item in result["evidence"]
        ), "expected real validation/test evidence in worker result"