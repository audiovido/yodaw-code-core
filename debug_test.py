import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import poll_mission

client = TestClient(app)

def run(cmd, cwd):
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )

def create_repo(root: Path):
    repo = root / "repo"
    repo.mkdir()

    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test"], repo)

    (repo / "app.py").write_text(
        "def greet(name):\n"
        "    return f'Hello {name}'\n"
    )

    (repo / "test_app.py").write_text(
        "from app import greet\n\n"
        "def test_greet():\n"
        "    assert greet('Armin') == 'Hello Armin'\n"
    )

    (repo / "pytest.ini").write_text(
        "[pytest]\n"
        "pythonpath = .\n"
    )

    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "baseline"], repo)
    return repo

with tempfile.TemporaryDirectory() as td:
    repo = create_repo(Path(td))

    response = client.post(
        "/api/v1/missions",
        json={
            "goal": "Refactor greeting without changing behavior",
            "capability": "repo-code",
            "metadata": {
                "repo_path": str(repo),
                "target_file": "app.py",
                "find": "return f'Hello {name}'",
                "replace": "return 'Hello ' + name",
                "commit_message": "refactor greeting implementation",
            },
        },
    )

    print("Response status:", response.status_code)
    print("Response JSON:", response.json())

    queued = response.json()
    print("Queued status:", queued["status"])

    payload = poll_mission(client, queued["id"])
    print("Final payload:", payload)