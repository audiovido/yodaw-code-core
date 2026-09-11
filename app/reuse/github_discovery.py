import os
import httpx
from typing import Optional

GITHUB_API = "https://api.github.com"


class GitHubDiscoveryError(RuntimeError):
    pass


def _headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "YODAW-Code-Core",
    }

    token = os.environ.get("GITHUB_TOKEN")

    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def search_repositories(
    query: str,
    *,
    language: Optional[str] = None,
    limit: int = 5,
) -> list[dict]:
    q = query

    if language:
        q += f" language:{language}"

    params = {
        "q": q,
        "sort": "stars",
        "order": "desc",
        "per_page": max(1, min(limit, 10)),
    }

    try:
        response = httpx.get(
            f"{GITHUB_API}/search/repositories",
            params=params,
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()

    except Exception as exc:
        raise GitHubDiscoveryError(
            f"GitHub search failed: {exc}"
        ) from exc

    items = response.json().get("items", [])

    results = []

    for item in items:
        license_info = item.get("license") or {}

        results.append(
            {
                "name": item.get("name"),
                "full_name": item.get("full_name"),
                "html_url": item.get("html_url"),
                "description": item.get("description"),
                "language": item.get("language"),
                "stars": item.get("stargazers_count", 0),
                "forks": item.get("forks_count", 0),
                "open_issues": item.get("open_issues_count", 0),
                "archived": item.get("archived", False),
                "fork": item.get("fork", False),
                "updated_at": item.get("updated_at"),
                "pushed_at": item.get("pushed_at"),
                "license": license_info.get("spdx_id"),
                "default_branch": item.get("default_branch"),
            }
        )

    return results
