#!/usr/bin/env python3.12
"""
YODAW Clean Machine Validation Script

This script validates that YODAW can be installed and run on a clean machine
by simulating a fresh environment.

Validation steps:
1. Create fresh temporary directory
2. Get fresh checkout (use current source or clone)
3. Run bootstrap/install
4. Check version/status
5. Start entrypoint
6. Verify health ready
7. Submit deterministic smoke mission (dry_run)
8. Observe terminal result
9. Stop cleanly
"""
import os
import sys
import subprocess
import shutil
import tempfile
import time
import json
from pathlib import Path

def run_command(cmd, cwd=None, env=None, check=True, capture_output=True):
    """Run a command and return the result."""
    print(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=capture_output,
        text=True
    )
    if check and result.returncode != 0:
        print(f"Command failed with exit code {result.returncode}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result

def check_health(base_url, timeout=30):
    """Check if YODAW is healthy."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = run_command(
                ["curl", "-s", f"{base_url}/api/v1/health"],
                check=False,
                capture_output=True
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                if data.get("status") == "READY":
                    return True, data
        except (json.JSONDecodeError, KeyError):
            pass
        time.sleep(1)
    return False, None

def main():
    # Configuration
    source_dir = Path.cwd()  # Use current worktree as source
    install_base = Path(tempfile.mkdtemp(prefix="yodaw_clean_machine_"))
    install_dir = install_base / "yodaw"
    
    print(f"YODAW Clean Machine Validation")
    print(f"Source directory: {source_dir}")
    print(f"Installation base: {install_base}")
    print(f"Installation directory: {install_dir}")
    
    try:
        # Step 1: Create fresh temporary directory (already done via mkdtemp)
        print("\n=== Step 1: Created fresh temporary directory ===")
        
        # Step 2: Get fresh checkout (we're using the current worktree)
        # For a truly clean machine, we might want to clone from a fresh archive
        # But for validation purposes, using current worktree is acceptable
        print("\n=== Step 2: Using current source as fresh checkout ===")
        print(f"Commit: {subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source_dir, text=True).strip()}")
        
        # Step 3: Run bootstrap/install
        print("\n=== Step 3: Running bootstrap/install ===")
        env = os.environ.copy()
        # Ensure we use python3.12
        env["PATH"] = f"/usr/local/opt/python@3.12/bin:{env.get('PATH', '')}"
        
        result = run_command([
            sys.executable, "scripts/bootstrap.py",
            "--source", str(source_dir),
            "--install-dir", str(install_dir)
        ], cwd=source_dir, env=env)
        
        # Verify installation files exist
        yodaw_bin = install_dir / "bin" / "yodaw"
        yodaw_uninstall = install_dir / "bin" / "yodaw-uninstall"
        version_file = install_dir / "lib" / "yodaw" / "version.py"
        
        assert yodaw_bin.exists(), f"YODAW wrapper not found at {yodaw_bin}"
        assert yodaw_uninstall.exists(), f"YODAW uninstall script not found at {yodaw_uninstall}"
        assert version_file.exists(), f"Version file not found at {version_file}"
        
        print(f"✓ Installation verified at {install_dir}")
        
        # Step 4: Check version/status
        print("\n=== Step 4: Checking version/status ===")
        version_content = version_file.read_text()
        print(f"Version file content:\n{version_content}")
        
        # Extract version info
        version_line = [line for line in version_content.split('\n') if '__version__' in line][0]
        version = version_line.split('=')[1].strip().strip('"')
        print(f"✓ Version: {version}")
        
        # Step 5: Start entrypoint
        print("\n=== Step 5: Starting entrypoint ===")
        # Start YODAW in background
        yodaw_process = subprocess.Popen(
            [str(yodaw_bin)],
            cwd=install_dir / "lib" / "yodaw",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
        
        # Give it a moment to start
        time.sleep(2)
        
        # Check if process is still alive
        if yodaw_process.poll() is not None:
            output = yodaw_process.stdout.read().decode()
            raise RuntimeError(f"YODAW process died immediately:\n{output}")
        
        print(f"✓ YODAW started with PID {yodaw_process.pid}")
        
        # Step 6: Verify health ready
        print("\n=== Step 6: Verifying health ready ===")
        base_url = "http://127.0.0.1:8844"
        healthy, health_data = check_health(base_url)
        
        if not healthy:
            # Get logs to debug
            stdout, _ = yodaw_process.communicate(timeout=5)
            raise RuntimeError(f"YODAW did not become healthy. Output:\n{stdout.decode()}")
        
        print(f"✓ YODAW is healthy: {health_data}")
        
        # Step 7: Submit deterministic smoke mission (using dry_run)
        print("\n=== Step 7: Submitting deterministic smoke mission ===")
        mission_goal = "Clean machine validation smoke test"
        mission_payload = {
            "goal": mission_goal,
            "capability": "repo-code",
            "dry_run": True  # This ensures no external dependencies
        }
        
        result = run_command([
            "curl", "-s", "-X", "POST", f"{base_url}/api/v1/missions",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(mission_payload)
        ], check=False)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit mission: {result.stderr}")
        
        mission_data = json.loads(result.stdout)
        mission_id = mission_data.get("id")
        initial_status = mission_data.get("status")
        
        assert mission_id, f"No mission ID in response: {mission_data}"
        print(f"✓ Submitted mission {mission_id} with status {initial_status}")
        
        # Step 8: Observe terminal result
        print("\n=== Step 8: Observing terminal result ===")
        start_time = time.time()
        timeout = 30  # seconds
        final_status = None
        final_mission = None
        
        while time.time() - start_time < timeout:
            result = run_command([
                "curl", "-s", f"{base_url}/api/v1/missions/{mission_id}"
            ], check=False)
            
            if result.returncode == 0:
                mission_data = json.loads(result.stdout)
                status = mission_data.get("status")
                
                if status in ["PASS", "FAIL", "BLOCKED", "CANCELLED", "BLOCKED_EXTERNAL"]:
                    final_status = status
                    final_mission = mission_data
                    break
                
                print(f"  Mission status: {status} (waiting...)")
                time.sleep(2)
            else:
                print(f"  Error checking mission status: {result.stderr}")
                time.sleep(2)
        
        assert final_status is not None, f"Mission did not complete within {timeout} seconds"
        assert final_mission is not None, "No mission data collected"
        
        print(f"✓ Mission completed with status: {final_status}")
        
        # For dry_run missions, we expect PASS
        if final_mission.get("dry_run"):
            assert final_status == "PASS", f"Dry run mission should PASS, got {final_status}"
            print("✓ Dry run mission passed as expected")
        
        # Step 9: Stop cleanly
        print("\n=== Step 9: Stopping cleanly ===")
        # Send SIGTERM to the process
        yodaw_process.terminate()
        
        try:
            yodaw_process.wait(timeout=10)
            print(f"✓ YODAW stopped cleanly with exit code {yodaw_process.returncode}")
        except subprocess.TimeoutExpired:
            print("⚠ YODAW did not stop gracefully, forcing kill")
            yodaw_process.kill()
            yodaw_process.wait()
            print(f"✓ YODAW killed with exit code {yodaw_process.returncode}")
        
        # Final validation
        print("\n=== Validation Complete ===")
        print("✓ All steps completed successfully")
        print(f"✓ Installation: {install_dir}")
        print(f"✓ Version: {version}")
        print(f"✓ Mission ID: {mission_id}")
        print(f"✓ Final Status: {final_status}")
        
        return True
        
    except Exception as e:
        print(f"\n✗ Validation failed: {e}")
        # Try to clean up the process if it's still running
        if 'yodaw_process' in locals() and yodaw_process.poll() is None:
            yodaw_process.kill()
            yodaw_process.wait()
        return False
    
    finally:
        # Cleanup installation directory
        print(f"\nCleaning up installation directory: {install_base}")
        shutil.rmtree(install_base, ignore_errors=True)

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)