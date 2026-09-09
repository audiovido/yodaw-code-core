from app.reuse.analyzer import (
    build_reuse_report,
    infer_github_search_terms,
)
from app.reuse.github_discovery import (
    search_repositories,
    GitHubDiscoveryError,
)
from app.reuse.quality_gate import rank_candidates


def build_full_reuse_intelligence(
    root,
    goal: str,
    *,
    enable_github: bool = True,
) -> dict:
    report = build_reuse_report(root)

    github = {
        "enabled": enable_github,
        "query": None,
        "candidates": [],
        "error": None,
    }

    if enable_github:
        search = infer_github_search_terms(
            goal,
            report["project_type"],
        )

        github["query"] = search

        try:
            candidates = search_repositories(
                search["query"],
                language=search["language"],
                limit=5,
            )

            github["candidates"] = rank_candidates(
                candidates
            )

        except GitHubDiscoveryError as exc:
            github["error"] = str(exc)

    return {
        **report,
        "github": github,
    }
