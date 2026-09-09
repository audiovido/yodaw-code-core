import subprocess
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


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


def test_repo_code_through_single_api():
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

        assert response.status_code == 200

        payload = response.json()

        assert payload["status"] == "PASS"
        assert payload["worker"] == "repo-code-bud"
        assert payload["result"]["tests_passed"] is True
        assert payload["result"]["commit_sha"]
