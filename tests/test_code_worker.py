import os
import tempfile
import shutil
from pathlib import Path
from app.workers.code_worker import CodeWorker


def test_real_code_worker_end_to_end():
    # Create a temporary git repository for testing
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_path = Path(temp_dir)
        
        # Initialize git repo
        os.system(f"cd {repo_path} && git init")
        os.system(f"cd {repo_path} && git config user.name 'Test User'")
        os.system(f"cd {repo_path} && git config user.email 'test@example.com'")
        
        # Create a test file
        test_file = repo_path / "test.py"
        original_content = "def hello():\n    return 'world'\n"
        test_file.write_text(original_content)
        
        # Initial commit
        os.system(f"cd {repo_path} && git add .")
        os.system(f"cd {repo_path} && git commit -m 'Initial commit'")
        
        worker = CodeWorker()
        
        # Define the new content: add a multiply function after the hello function
        new_content = original_content + "\n\ndef multiply(a, b):\n    return a * b\n"
        
        # Execute with repository context and explicit edits
        result = worker.execute(
            "Add a multiply function",
            metadata={
                "repo_path": str(repo_path),
                "edits": [{
                    "target_file": "test.py",
                    "find": original_content,
                    "replace": new_content,
                }]
            }
        )
        
        # Print result for debugging if needed
        print("Result:", result)
        
        # Verify the result contains real evidence
        assert result["success"] is True
        # These should now be real values from the RepoCodeWorker execution
        assert "tests_passed" in result["output"]
        assert result["output"]["tests_passed"] is True  # Since no test commands, validation passes
        assert "commit_sha" in result["output"]
        assert len(result["output"]["commit_sha"]) == 40  # SHA-1 length
        assert "working_tree_clean" in result["output"]
        assert result["output"]["working_tree_clean"] is True
        assert len(result["evidence"]) >= 5  # We should have several evidence items