"""
Task classification: deterministic-first classifier that identifies coding intent.
"""
import re
from typing import Optional
from app.skills.protocols import SkillIntent, SkillClassification
from app.llm.provider import LocalLLMProvider, LLMError


# Deterministic classification patterns
BUGFIX_PATTERNS = [
    r"\bfix\b",
    r"\bbug\b",
    r"\berror\b",
    r"\bcrash\b",
    r"\bfail(ing|ure|ed)?\b",
    r"\bbroke(n)?\b",
    r"\bissue\b",
    r"\bdefect\b",
    r"\bregression\b",
]

REFACTOR_PATTERNS = [
    r"\brefactor\b",
    r"\bclean\s*up\b",
    r"\brestructure\b",
    r"\breorganize\b",
    r"\bsimplify\b",
    r"\bextract\b.*\bmethod\b",
    r"\brename\b",
    r"\bdedup(licate)?\b",
]

TEST_PATTERNS = [
    r"\btest\b",
    r"\bunit\s*test\b",
    r"\bintegration\s*test\b",
    r"\be2e\b",
    r"\bcoverage\b",
    r"\bspec\b",
    r"\bassertion\b",
]

FEATURE_PATTERNS = [
    r"\badd\b",
    r"\bimplement\b",
    r"\bcreate\b",
    r"\bbuild\b",
    r"\bnew\s+(feature|functionality|capability)\b",
    r"\bsupport\b.*\bfor\b",
]

REVIEW_PATTERNS = [
    r"\breview\b",
    r"\baudit\b",
    r"\binspect\b",
    r"\bcheck\b.*\bfor\b",
    r"\banalyze\b",
]

DOCUMENTATION_PATTERNS = [
    r"\bdoc(ument|s)?\b",
    r"\breadme\b",
    r"\bcomment\b",
    r"\bdescribe\b",
    r"\bexplain\b",
]

DEPENDENCY_PATTERNS = [
    r"\bdependen(cy|cies)\b",
    r"\blibrary\b",
    r"\bpackage\b",
    r"\bupgrade\b",
    r"\binstall\b",
    r"\breuse\b",
]

MIGRATION_PATTERNS = [
    r"\bmigrat(e|ion)\b",
    r"\bport\b.*\bto\b",
    r"\bconvert\b",
    r"\bupdate\b.*\bversion\b",
]

PERFORMANCE_PATTERNS = [
    r"\bperformance\b",
    r"\boptimize\b",
    r"\bspeed\s*up\b",
    r"\bcache\b",
    r"\bslow\b",
    r"\bbottleneck\b",
]

SECURITY_PATTERNS = [
    r"\bsecur(e|ity)\b",
    r"\bvulnerability\b",
    r"\bauth(entication|orization)?\b",
    r"\bexploit\b",
    r"\binjection\b",
]


def deterministic_classify(goal: str) -> Optional[SkillClassification]:
    """
    Classify task intent using deterministic pattern matching.
    Returns None if ambiguous or no clear match.
    """
    goal_lower = goal.lower()
    
    scores = {}
    
    for pattern in BUGFIX_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.BUGFIX] = scores.get(SkillIntent.BUGFIX, 0) + 1
    
    for pattern in REFACTOR_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.REFACTOR] = scores.get(SkillIntent.REFACTOR, 0) + 1
    
    for pattern in TEST_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.TEST] = scores.get(SkillIntent.TEST, 0) + 1
    
    for pattern in FEATURE_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.FEATURE] = scores.get(SkillIntent.FEATURE, 0) + 1
    
    for pattern in REVIEW_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.REVIEW] = scores.get(SkillIntent.REVIEW, 0) + 1
    
    for pattern in DOCUMENTATION_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.DOCUMENTATION] = scores.get(SkillIntent.DOCUMENTATION, 0) + 1
    
    for pattern in DEPENDENCY_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.DEPENDENCY] = scores.get(SkillIntent.DEPENDENCY, 0) + 1
    
    for pattern in MIGRATION_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.MIGRATION] = scores.get(SkillIntent.MIGRATION, 0) + 1
    
    for pattern in PERFORMANCE_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.PERFORMANCE] = scores.get(SkillIntent.PERFORMANCE, 0) + 1
    
    for pattern in SECURITY_PATTERNS:
        if re.search(pattern, goal_lower):
            scores[SkillIntent.SECURITY] = scores.get(SkillIntent.SECURITY, 0) + 1
    
    if not scores:
        return None
    
    sorted_skills = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    
    primary = sorted_skills[0][0]
    primary_score = sorted_skills[0][1]
    
    # Multiple high-scoring skills suggest mixed intent
    # Use stricter threshold: secondary must be at least 50% of primary
    # and both must have at least 2 signals
    if len(sorted_skills) > 1:
        secondary_score = sorted_skills[1][1]
        if secondary_score >= 2 and secondary_score >= primary_score * 0.5:
            secondary = [skill for skill, score in sorted_skills[1:] if score >= 2]
            return SkillClassification(
                primary_skill=SkillIntent.MIXED,
                secondary_skills=[primary] + secondary[:2],
                confidence=0.7,
                reason=f"multiple strong signals: {[s.value for s in [primary] + secondary[:2]]}",
            )
    
    secondary = [skill for skill, _ in sorted_skills[1:3]]
    
    return SkillClassification(
        primary_skill=primary,
        secondary_skills=secondary,
        confidence=0.85,
        reason=f"deterministic match: {primary_score} signals",
    )


LLM_CLASSIFICATION_PROMPT = """
You are a task classifier for a coding agent.

Your job is to classify the coding goal into ONE primary skill intent.

Valid intents:
- bugfix: fixing errors, crashes, failures
- refactor: restructuring without behavior change
- feature: adding new functionality
- test: writing or improving tests
- review: analyzing code without modification
- documentation: writing docs, comments, README
- dependency: managing libraries, packages
- migration: porting, converting, upgrading
- performance: optimization, caching
- security: vulnerability fixes, auth
- mixed: multiple equally important intents

Return JSON only:

{
  "primary_skill": "bugfix",
  "secondary_skills": ["test"],
  "confidence": 0.9,
  "reason": "short explanation"
}

Never wrap JSON in markdown.
""".strip()


def llm_classify(goal: str, provider=None) -> SkillClassification:
    """
    Fallback LLM-based classification for ambiguous tasks.
    """
    provider = provider or LocalLLMProvider()
    
    user_prompt = f"CODING GOAL:\n\n{goal}\n\nClassify this task."
    
    raw = provider.chat(LLM_CLASSIFICATION_PROMPT, user_prompt)
    
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    
    import json
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Classifier returned invalid JSON: {exc}") from exc
    
    primary = result.get("primary_skill")
    if not primary or primary not in [intent.value for intent in SkillIntent]:
        raise LLMError(f"Invalid primary_skill: {primary}")
    
    return SkillClassification(
        primary_skill=SkillIntent(primary),
        secondary_skills=[
            SkillIntent(s) for s in result.get("secondary_skills", [])
            if s in [intent.value for intent in SkillIntent]
        ],
        confidence=float(result.get("confidence", 0.5)),
        reason=result.get("reason", "llm classification"),
    )


def classify_task(goal: str, provider=None) -> SkillClassification:
    """
    Classify coding task intent using deterministic patterns first,
    falling back to LLM only when ambiguous.
    """
    deterministic_result = deterministic_classify(goal)
    
    if deterministic_result is not None and deterministic_result.confidence >= 0.8:
        return deterministic_result
    
    # Ambiguous or no match: use LLM
    return llm_classify(goal, provider)
