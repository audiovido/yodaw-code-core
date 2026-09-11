import re
from typing import Optional
from app.learning.store import LearningStore


_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{2,}")

STOPWORDS = {
    "the", "and", "for", "with", "without", "that", "this", "from",
    "into", "when", "then", "should", "must", "have", "has", "not",
    "use", "using", "make", "made", "all", "any", "can", "will",
    "a", "an", "to", "of", "in", "on", "is", "are", "be", "it",
}


def tokenize(text: str) -> set[str]:
    return {
        token.lower()
        for token in _TOKEN_RE.findall(text or "")
        if token.lower() not in STOPWORDS
    }


def format_lessons(records) -> list[str]:
    """
    Render retrieved records as human/LLM-readable lessons.
    Guidance only — never authoritative truth or policy.
    """
    lessons = []

    for record in records:
        parts = []

        if record.outcome == "PASS" and record.strategy:
            parts.append(f"validated strategy: {record.strategy}")
        elif record.outcome == "FAIL" and record.root_cause:
            parts.append(f"previous failure: {record.root_cause}")

        if record.solution and record.outcome == "PASS":
            if record.solution not in parts:
                parts.append(f"solution: {record.solution}")

        if record.tools:
            parts.append(f"tools: {', '.join(record.tools)}")

        if record.outcome == "PASS":
            outcome_note = "prior success"
        else:
            outcome_note = "prior failure"

        lessons.append(
            f"- [{outcome_note}] goal '{record.goal}': "
            + "; ".join(parts)
            if parts
            else f"- [{outcome_note}] goal '{record.goal}'"
        )

    return lessons


def retrieve_relevant_learnings(
    goal: str,
    limit: int = 3,
    store: Optional[LearningStore] = None,
):
    """
    Deterministic lexical-overlap ranking over prior learning
    records. Guidance for the planner, not authoritative truth.
    """
    store = store or LearningStore()

    goal_tokens = tokenize(goal)

    if not goal_tokens:
        return []

    scored = []

    try:
        records = store.list()
    except Exception:
        # Retrieval must never corrupt a mission.
        return []

    for record in records:
        record_tokens = tokenize(
            " ".join(
                filter(
                    None,
                    [
                        record.goal,
                        record.worker or "",
                        record.strategy or "",
                        record.root_cause or "",
                        record.solution or "",
                        " ".join(record.tools),
                        " ".join(record.tags),
                    ],
                ),
            )
        )

        if not record_tokens:
            continue

        overlap = goal_tokens & record_tokens
        score = len(overlap)
        if record.outcome == "PASS":
            score += 1

        if score > 1:
            scored.append((score, record))

    scored.sort(
        key=lambda item: (-item[0], item[1].created_at),
    )

    return [record for _, record in scored[:max(0, int(limit))]]
