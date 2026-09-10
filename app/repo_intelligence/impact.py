"""Impact analysis via reverse dependency graph."""

from .graph import reverse_dependencies
from .models import ImportEdge


def blast_radius(changed: list[str], edges: list[ImportEdge], max_depth: int = 10) -> dict[str, list[str]]:
    """Map each changed file to its affected dependents (BFS, sorted)."""
    rev = reverse_dependencies(edges)
    result: dict[str, list[str]] = {}
    for seed in sorted(set(changed)):
        seen: set[str] = set()
        frontier = [seed]
        depth = 0
        while frontier and depth < max_depth:
            nxt: list[str] = []
            for node in frontier:
                for dep in rev.get(node, []):
                    if dep not in seen and dep != seed:
                        seen.add(dep)
                        nxt.append(dep)
            frontier = sorted(set(nxt))
            depth += 1
        result[seed] = sorted(seen)
    return result
