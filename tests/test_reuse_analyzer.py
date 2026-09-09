import json
import tempfile
from pathlib import Path

from app.reuse.analyzer import (
    build_reuse_report,
    detect_project_type,
    read_node_dependencies,
    read_python_dependencies,
)


def test_detect_python_project():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "requirements.txt").write_text(
            "fastapi\n"
        )

        result = detect_project_type(root)

        assert result["python"] is True


def test_read_python_dependencies():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "requirements.txt").write_text(
            """
fastapi==0.141.1
httpx>=0.28
# ignored
"""
        )

        deps = read_python_dependencies(root)

        assert "fastapi==0.141.1" in deps
        assert "httpx>=0.28" in deps


def test_read_node_dependencies():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "package.json").write_text(
            json.dumps(
                {
                    "dependencies": {
                        "react": "^19.0.0"
                    },
                    "devDependencies": {
                        "vite": "^7.0.0"
                    },
                }
            )
        )

        deps = read_node_dependencies(root)

        assert "react@^19.0.0" in deps
        assert "vite@^7.0.0" in deps


def test_reuse_report_detects_existing_module():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        (root / "requirements.txt").write_text(
            "fastapi\n"
        )

        (root / "auth.py").write_text(
            "def login():\n"
            "    pass\n"
        )

        report = build_reuse_report(root)

        assert report["project_type"]["python"] is True
        assert "auth.py" in report["existing_reusable_modules"]
        assert len(report["policy"]) >= 1
