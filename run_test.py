import subprocess
import sys
import threading
import time

def run_test():
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", "test_serving_path_graduation.py::TestServingPathGraduation::test_local_worker_execution", "-v", "-s"],
        cwd="/Users/arminshokri/yodaw-graduation-tests",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )
    # Wait for 30 seconds
    try:
        outs, _ = proc.communicate(timeout=30)
        print(outs)
        return proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        outs, _ = proc.communicate()
        print("Test timed out after 30 seconds")
        print(outs)
        return -1

if __name__ == "__main__":
    sys.exit(run_test())