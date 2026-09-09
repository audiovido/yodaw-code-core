import tempfile
from pathlib import Path

import app.reuse.service as service


def test_full_reuse_intelligence_without_network():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "requirements.txt").write_text(
            "fastapi\n"
        )

        result = service.build_full_reuse_intelligence(
            root,
            "add authentication",
            enable_github=False,
        )

        assert result["project_type"]["python"] is True
        assert result["github"]["enabled"] is False


def test_full_reuse_intelligence_with_fake_github(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "requirements.txt").write_text(
            "fastapi\n"
        )

        def fake_search(query, language=None, limit=5):
            return [
                {
                    "name": "authlib",
                    "full_name": "org/authlib",
                    "html_url": "https://github.com/org/authlib",
                    "description": "auth",
                    "language": "Python",
                    "stars": 5000,
                    "forks": 500,
                    "open_issues": 10,
                    "archived": False,
                    "fork": False,
                    "updated_at": "2099-01-01T00:00:00Z",
                    "pushed_at": "2099-01-01T00:00:00Z",
                    "license": "MIT",
                    "default_branch": "main",
                }
            ]

        monkeypatch.setattr(
            service,
            "search_repositories",
            fake_search,
        )

        result = service.build_full_reuse_intelligence(
            root,
            "add authentication",
        )

        candidates = result["github"]["candidates"]

        assert len(candidates) == 1
        assert candidates[0]["decision"] in {
            "STRONG",
            "REVIEW",
        }
