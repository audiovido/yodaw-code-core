from datetime import datetime, timezone


ALLOWED_LICENSES = {
    "MIT",
    "Apache-2.0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "ISC",
}


def _days_since(iso_value: str | None) -> int | None:
    if not iso_value:
        return None

    try:
        dt = datetime.fromisoformat(
            iso_value.replace("Z", "+00:00")
        )
        now = datetime.now(timezone.utc)
        return (now - dt).days

    except Exception:
        return None


def score_candidate(candidate: dict) -> dict:
    score = 0
    reasons = []
    blockers = []

    stars = int(candidate.get("stars") or 0)
    forks = int(candidate.get("forks") or 0)
    archived = bool(candidate.get("archived"))
    license_id = candidate.get("license")
    pushed_days = _days_since(candidate.get("pushed_at"))

    if archived:
        blockers.append("repository is archived")

    if license_id not in ALLOWED_LICENSES:
        blockers.append(
            f"license not approved: {license_id}"
        )

    if stars >= 10000:
        score += 30
        reasons.append("very strong adoption signal")
    elif stars >= 1000:
        score += 24
        reasons.append("strong adoption signal")
    elif stars >= 100:
        score += 16
        reasons.append("moderate adoption signal")
    elif stars >= 20:
        score += 8
        reasons.append("some adoption signal")

    if forks >= 500:
        score += 15
    elif forks >= 100:
        score += 10
    elif forks >= 20:
        score += 5

    if pushed_days is not None:
        if pushed_days <= 90:
            score += 25
            reasons.append("recently maintained")
        elif pushed_days <= 365:
            score += 18
            reasons.append("maintained within a year")
        elif pushed_days <= 730:
            score += 8
            reasons.append("maintenance is aging")
        else:
            reasons.append("stale repository")

    if license_id in ALLOWED_LICENSES:
        score += 20
        reasons.append(f"approved license: {license_id}")

    decision = "REJECT" if blockers else (
        "STRONG" if score >= 70
        else "REVIEW" if score >= 45
        else "WEAK"
    )

    return {
        **candidate,
        "quality_score": score,
        "decision": decision,
        "reasons": reasons,
        "blockers": blockers,
    }


def rank_candidates(candidates: list[dict]) -> list[dict]:
    scored = [
        score_candidate(candidate)
        for candidate in candidates
    ]

    return sorted(
        scored,
        key=lambda x: x["quality_score"],
        reverse=True,
    )
