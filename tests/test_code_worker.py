import os
import tempfile
import subprocess
from app.workers.code_worker import CodeWorker


def test_real_code_worker_end_to_end():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmpdir, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmpdir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)

        # Create a pytest.ini to trigger test detection
        pytest_ini = os.path.join(tmpdir, "pytest.ini")
        with open(pytest_ini, "w") as f:
            f.write("[pytest]\n")

        # Create a dummy test file in tests/
        tests_dir = os.path.join(tmpdir, "tests")
        os.makedirs(tests_dir)
        test_file = os.path.join(tests_dir, "test_dummy.py")
        with open(test_file, "w") as f:
            f.write("def test_dummy():\n    assert True\n")

        # Create hello.txt with original content
        hello_file = os.path.join(tmpdir, "hello.txt")
        with open(hello_file, "w") as f:
            f.write("original\n")

        # Commit the initial state
        subprocess.run(["git", "add", "."], cwd=tmpdir, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=tmpdir, check=True)

        worker = CodeWorker()
        result = worker.execute(
            "Modify hello.txt to say Hello, World!",
            metadata={"repo_path": tmpdir}
        )
        print("DEBUG: result =", result)
        assert result["success"] is True
        assert result["output"]["tests_passed"] is True
        assert result["output"]["commit_sha"]
        assert result["output"]["working_tree_clean"] is True
        assert len(result["evidence"]) >= 5