import json
from pathlib import Path


def detect_project_type(root: Path) -> dict:
    result = {
        "python": False,
        "node": False,
        "rust": False,
        "go": False,
        "java": False,
        "swift": False,
    }

    if any(
        (root / name).exists()
        for name in [
            "pyproject.toml",
            "requirements.txt",
            "setup.py",
            "pytest.ini",
        ]
    ):
        result["python"] = True

    if (root / "package.json").exists():
        result["node"] = True

    if (root / "Cargo.toml").exists():
        result["rust"] = True

    if (root / "go.mod").exists():
        result["go"] = True

    if (root / "pom.xml").exists() or (root / "build.gradle").exists():
        result["java"] = True

    if (root / "Package.swift").exists():
        result["swift"] = True

    return result


def read_python_dependencies(root: Path) -> list[str]:
    deps = []

    requirements = root / "requirements.txt"

    if requirements.exists():
        for line in requirements.read_text(errors="replace").splitlines():
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            deps.append(line)

    pyproject = root / "pyproject.toml"

    if pyproject.exists():
        text = pyproject.read_text(errors="replace")

        for line in text.splitlines():
            stripped = line.strip()

            if (
                stripped.startswith('"')
                and any(x in stripped for x in [">=", "==", "~=", "<=", ">"])
            ):
                deps.append(stripped.strip(",").strip('"'))

    return sorted(set(deps))


def read_node_dependencies(root: Path) -> list[str]:
    package = root / "package.json"

    if not package.exists():
        return []

    try:
        data = json.loads(package.read_text())
    except Exception:
        return []

    deps = {}

    deps.update(data.get("dependencies", {}))
    deps.update(data.get("devDependencies", {}))

    return sorted(
        f"{name}@{version}"
        for name, version in deps.items()
    )


def find_existing_reusable_modules(root: Path) -> list[str]:
    candidates = []

    interesting_names = {
        "auth",
        "authentication",
        "database",
        "db",
        "http",
        "client",
        "api",
        "cache",
        "logging",
        "logger",
        "config",
        "utils",
        "helpers",
        "security",
        "storage",
        "queue",
        "worker",
        "service",
        "services",
    }

    ignored = {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        "dist",
        "build",
    }

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        if any(part in ignored for part in path.parts):
            continue

        stem = path.stem.lower()

        if stem in interesting_names:
            try:
                candidates.append(
                    str(path.relative_to(root))
                )
            except Exception:
                pass

    return sorted(set(candidates))


def build_reuse_report(root: Path) -> dict:
    project_type = detect_project_type(root)

    return {
        "project_type": project_type,
        "python_dependencies": read_python_dependencies(root),
        "node_dependencies": read_node_dependencies(root),
        "existing_reusable_modules": find_existing_reusable_modules(root),
        "policy": [
            "Reuse existing project code before creating duplicate modules.",
            "Reuse existing installed dependencies before adding new ones.",
            "Prefer mature libraries over bespoke implementations for common infrastructure.",
            "Do not add a dependency unless existing project code and installed dependencies are insufficient.",
        ],
    }


def infer_github_search_terms(goal: str, project_type: dict) -> dict:
    language = None

    if project_type.get("python"):
        language = "Python"
    elif project_type.get("node"):
        language = "TypeScript"
    elif project_type.get("rust"):
        language = "Rust"
    elif project_type.get("go"):
        language = "Go"
    elif project_type.get("java"):
        language = "Java"
    elif project_type.get("swift"):
        language = "Swift"

    cleaned = " ".join(
        part
        for part in goal.replace("/", " ").split()
        if len(part) > 2
    )

    return {
        "query": cleaned[:120],
        "language": language,
    }
