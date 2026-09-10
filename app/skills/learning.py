"""
Skill-aware learning system for YODAW Coder Skills.

Learns from skill execution evidence to improve future skill selection,
planning, and execution.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from collections import defaultdict

from .models import SkillId, Intent, ClassificationResult, SelectionResult, SkillEvidence, Skill, RiskLevel
from .selection import SelectionContext
from .registry import SkillRegistry
from .evidence import EvidenceStore, EvidenceAggregator


@dataclass
class LearningPattern:
    """A learned pattern from execution history."""
    pattern_id: str
    skill_id: SkillId
    intent: Intent
    context_signature: dict[str, Any]
    success_rate: float
    avg_duration_minutes: float
    common_errors: list[str]
    effective_strategies: list[str]
    sample_count: int
    last_updated: str
    confidence: float


@dataclass
class SkillPerformanceProfile:
    """Performance profile for a skill."""
    skill_id: SkillId
    total_executions: int
    success_rate: float
    avg_duration_minutes: float
    duration_stddev: float
    common_failure_modes: dict[str, int]
    effective_contexts: list[dict[str, Any]]
    problematic_contexts: list[dict[str, Any]]
    recommended_preconditions: list[str]
    last_updated: str


class SkillLearningEngine:
    """Learns from skill execution evidence."""

    def __init__(self, registry: SkillRegistry, evidence_store: EvidenceStore):
        self.registry = registry
        self.evidence_store = evidence_store
        self.aggregator = EvidenceAggregator(evidence_store)
        self._patterns: dict[str, LearningPattern] = {}
        self._profiles: dict[SkillId, SkillPerformanceProfile] = {}

    def learn_from_execution(self, evidence: SkillEvidence) -> list[LearningPattern]:
        """Learn from a single skill execution."""
        new_patterns = []

        # Update skill profile
        profile = self._update_skill_profile(evidence.skill_id)
        if profile:
            self._profiles[evidence.skill_id] = profile

        # Extract patterns from successful executions
        if evidence.success:
            pattern = self._extract_success_pattern(evidence)
            if pattern:
                self._patterns[pattern.pattern_id] = pattern
                new_patterns.append(pattern)

        # Extract failure patterns
        if not evidence.success and evidence.errors:
            pattern = self._extract_failure_pattern(evidence)
            if pattern:
                self._patterns[pattern.pattern_id] = pattern
                new_patterns.append(pattern)

        return new_patterns

    def _update_skill_profile(self, skill_id: SkillId) -> Optional[SkillPerformanceProfile]:
        """Update performance profile for a skill."""
        stats = self.aggregator.get_skill_statistics(skill_id)

        if stats["total_executions"] < 3:
            return None  # Not enough data

        records = self.evidence_store.get_by_skill(skill_id, limit=1000)

        # Calculate duration statistics
        durations = []
        failure_modes = defaultdict(int)
        success_contexts = []
        failure_contexts = []

        for record in records:
            if "duration_minutes" in record.metrics:
                durations.append(record.metrics["duration_minutes"])

            if not record.success:
                for error in record.errors:
                    failure_modes[error] += 1
                failure_contexts.append(record.inputs)
            else:
                success_contexts.append(record.inputs)

        avg_duration = sum(durations) / len(durations) if durations else 0
        duration_stddev = (sum((d - avg_duration) ** 2 for d in durations) / len(durations)) ** 0.5 if durations else 0

        # Find effective contexts (contexts with high success)
        effective = self._find_effective_contexts(success_contexts, failure_contexts)

        return SkillPerformanceProfile(
            skill_id=skill_id,
            total_executions=stats["total_executions"],
            success_rate=stats["success_rate"],
            avg_duration_minutes=avg_duration,
            duration_stddev=duration_stddev,
            common_failure_modes=dict(failure_modes),
            effective_contexts=effective["effective"],
            problematic_contexts=effective["problematic"],
            recommended_preconditions=self._derive_preconditions(records),
            last_updated=datetime.now().isoformat(),
        )

    def _find_effective_contexts(
        self,
        success_contexts: list[dict[str, Any]],
        failure_contexts: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Find contexts that correlate with success/failure."""
        # Simple heuristic: contexts that appear in successes but not failures
        success_keys = set()
        for ctx in success_contexts:
            for k, v in ctx.items():
                if isinstance(v, (str, int, float, bool)):
                    success_keys.add(f"{k}={v}")

        failure_keys = set()
        for ctx in failure_contexts:
            for k, v in ctx.items():
                if isinstance(v, (str, int, float, bool)):
                    failure_keys.add(f"{k}={v}")

        effective_keys = success_keys - failure_keys
        problematic_keys = failure_keys - success_keys

        effective_contexts = []
        for ctx in success_contexts:
            matched = sum(1 for k, v in ctx.items() if f"{k}={v}" in effective_keys)
            if matched > 0:
                effective_contexts.append(ctx)

        problematic_contexts = []
        for ctx in failure_contexts:
            matched = sum(1 for k, v in ctx.items() if f"{k}={v}" in problematic_keys)
            if matched > 0:
                problematic_contexts.append(ctx)

        return {
            "effective": effective_contexts[:10],
            "problematic": problematic_contexts[:10],
        }

    def _derive_preconditions(self, records: list) -> list[str]:
        """Derive recommended preconditions from execution history."""
        preconditions = []

        # Check for common prerequisites in successful runs
        success_prereqs = defaultdict(int)
        total_success = 0

        for record in records:
            if record.success:
                total_success += 1
                for key in record.inputs:
                    success_prereqs[key] += 1

        # If a prerequisite appears in >80% of successful runs, recommend it
        for prereq, count in success_prereqs.items():
            if total_success > 0 and count / total_success > 0.8:
                preconditions.append(f"Ensure {prereq} is provided")

        return preconditions[:5]

    def _extract_success_pattern(self, evidence: SkillEvidence) -> Optional[LearningPattern]:
        """Extract a success pattern from evidence."""
        skill = self.registry.get(evidence.skill_id)
        if not skill:
            return None

        # Create context signature from inputs
        context_sig = self._create_context_signature(evidence.inputs)

        pattern_id = f"success_{evidence.skill_id}_{hash(json.dumps(context_sig, sort_keys=True)) % 10000}"

        # Check if pattern exists
        existing = self._patterns.get(pattern_id)
        if existing:
            # Update existing pattern
            existing.sample_count += 1
            existing.success_rate = (existing.success_rate * (existing.sample_count - 1) + 1.0) / existing.sample_count
            existing.last_updated = datetime.now().isoformat()
            existing.confidence = min(existing.confidence + 0.05, 1.0)
            return existing

        return LearningPattern(
            pattern_id=pattern_id,
            skill_id=evidence.skill_id,
            intent=skill.supported_intents[0] if skill.supported_intents else Intent.FEATURE,
            context_signature=context_sig,
            success_rate=1.0,
            avg_duration_minutes=evidence.metrics.get("duration_minutes", 0),
            common_errors=[],
            effective_strategies=self._extract_strategies(evidence),
            sample_count=1,
            last_updated=datetime.now().isoformat(),
            confidence=0.5,
        )

    def _extract_failure_pattern(self, evidence: SkillEvidence) -> Optional[LearningPattern]:
        """Extract a failure pattern from evidence."""
        skill = self.registry.get(evidence.skill_id)
        if not skill:
            return None

        context_sig = self._create_context_signature(evidence.inputs)

        pattern_id = f"failure_{evidence.skill_id}_{hash(json.dumps(context_sig, sort_keys=True)) % 10000}"

        existing = self._patterns.get(pattern_id)
        if existing:
            existing.sample_count += 1
            existing.success_rate = (existing.success_rate * (existing.sample_count - 1)) / existing.sample_count
            existing.common_errors = list(set(existing.common_errors + evidence.errors))
            existing.last_updated = datetime.now().isoformat()
            existing.confidence = min(existing.confidence + 0.05, 1.0)
            return existing

        return LearningPattern(
            pattern_id=pattern_id,
            skill_id=evidence.skill_id,
            intent=skill.supported_intents[0] if skill.supported_intents else Intent.FEATURE,
            context_signature=context_sig,
            success_rate=0.0,
            avg_duration_minutes=evidence.metrics.get("duration_minutes", 0),
            common_errors=evidence.errors,
            effective_strategies=[],
            sample_count=1,
            last_updated=datetime.now().isoformat(),
            confidence=0.5,
        )

    def _create_context_signature(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Create a simplified context signature from inputs."""
        # Extract key context indicators
        signature = {}
        key_indicators = [
            "language", "framework", "project_size", "test_coverage",
            "has_tests", "has_ci", "complexity", "team_size",
        ]
        for key in key_indicators:
            if key in inputs:
                signature[key] = inputs[key]

        # Add input keys as presence indicators
        signature["input_keys"] = sorted(list(inputs.keys()))

        return signature

    def _extract_strategies(self, evidence: SkillEvidence) -> list[str]:
        """Extract effective strategies from successful execution."""
        strategies = []

        # Look for strategy indicators in outputs/metadata
        if "strategy" in evidence.outputs:
            strategies.append(evidence.outputs["strategy"])

        if "approach" in evidence.metadata:
            strategies.append(evidence.metadata["approach"])

        # Infer from artifacts
        for artifact_type in evidence.artifacts:
            if artifact_type not in strategies:
                strategies.append(f"produced_{artifact_type}")

        return strategies

    def get_recommendations(
        self,
        skill_id: SkillId,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Get learning-based recommendations for a skill in context."""
        profile = self._profiles.get(skill_id)
        if not profile:
            stats = self.aggregator.get_skill_statistics(skill_id)
            if stats["total_executions"] >= 3:
                profile = self._update_skill_profile(skill_id)

        recommendations = {
            "should_use": True,
            "confidence": 0.5,
            "estimated_duration": 30,
            "preconditions": [],
            "warnings": [],
            "strategies": [],
        }

        if profile:
            recommendations["confidence"] = profile.success_rate
            recommendations["estimated_duration"] = profile.avg_duration_minutes
            recommendations["preconditions"] = profile.recommended_preconditions

            # Check for problematic context match
            context_sig = self._create_context_signature(context)
            for prob_ctx in profile.problematic_contexts:
                if self._contexts_match(context_sig, prob_ctx):
                    recommendations["warnings"].append(
                        f"Similar context had issues: {prob_ctx}"
                    )
                    recommendations["confidence"] *= 0.7

            # Add effective strategies from patterns
            for pattern in self._patterns.values():
                if pattern.skill_id == skill_id and pattern.success_rate > 0.7:
                    if self._contexts_match(context_sig, pattern.context_signature):
                        recommendations["strategies"].extend(pattern.effective_strategies)

        return recommendations

    def _contexts_match(self, ctx1: dict[str, Any], ctx2: dict[str, Any]) -> bool:
        """Check if two contexts match."""
        if not ctx1 or not ctx2:
            return False

        matches = 0
        total = 0
        for key in set(ctx1.keys()) | set(ctx2.keys()):
            total += 1
            if ctx1.get(key) == ctx2.get(key):
                matches += 1

        return total > 0 and matches / total > 0.6

    def get_skill_ranking_adjustment(
        self,
        candidates: list[tuple[SkillId, float]],
        context: dict[str, Any],
    ) -> list[tuple[SkillId, float]]:
        """Adjust skill ranking based on learning."""
        adjusted = []

        for skill_id, base_score in candidates:
            rec = self.get_recommendations(skill_id, context)
            adjusted_score = base_score * rec["confidence"]

            # Boost if preconditions met
            preconditions_met = all(
                any(k in context for k in pc.split()) for pc in rec["preconditions"]
            )
            if preconditions_met and rec["preconditions"]:
                adjusted_score *= 1.1

            # Penalize for warnings
            adjusted_score *= (0.9 ** len(rec["warnings"]))

            adjusted.append((skill_id, adjusted_score))

        adjusted.sort(key=lambda x: x[1], reverse=True)
        return adjusted


class SkillAwarePlanner:
    """Planner that uses skill learning for better task planning."""

    def __init__(self, registry: SkillRegistry, learning_engine: SkillLearningEngine):
        self.registry = registry
        self.learning_engine = learning_engine

    def create_plan(
        self,
        task_description: str,
        classification: ClassificationResult,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a skill-aware execution plan."""
        primary_skill = self.registry.get(classification.primary_skill)

        if not primary_skill:
            return {"error": "Primary skill not found"}

        # Get learning recommendations
        rec = self.learning_engine.get_recommendations(classification.primary_skill, context)

        # Get skill chain
        from .selection import SkillSelector, SelectionContext
        selector = SkillSelector(self.registry)
        sel_context = SelectionContext(
            task_description=task_description,
            classification=classification,
            project_language=context.get("language"),
            project_frameworks=context.get("frameworks", []),
        )
        skill_chain = selector.get_skill_chain(sel_context)

        plan = {
            "primary_skill": {
                "id": primary_skill.id,
                "name": primary_skill.name,
                "estimated_duration": rec.get("estimated_duration", primary_skill.planning_guidance.estimated_duration_minutes),
                "confidence": rec["confidence"],
                "preconditions": rec["preconditions"],
                "warnings": rec["warnings"],
                "recommended_strategies": rec["strategies"],
            },
            "skill_chain": [
                {
                    "id": s.id,
                    "name": s.name,
                    "intent": [i.value for i in s.supported_intents],
                    "risk": s.risk.value,
                }
                for s in skill_chain
            ],
            "execution_strategy": self._determine_execution_strategy(primary_skill, rec, context),
            "risk_assessment": self._assess_plan_risk(primary_skill, rec, skill_chain),
            "checkpoints": self._generate_checkpoints(primary_skill),
        }

        return plan

    def _determine_execution_strategy(
        self,
        skill: Skill,
        recommendations: dict[str, Any],
        context: dict[str, Any],
    ) -> str:
        """Determine execution strategy based on learning."""
        if recommendations["confidence"] > 0.8 and not recommendations["warnings"]:
            return "autonomous"
        elif recommendations["confidence"] > 0.5:
            return "supervised"
        else:
            return "guided"

    def _assess_plan_risk(
        self,
        primary_skill: Skill,
        recommendations: dict[str, Any],
        skill_chain: list[Skill],
    ) -> dict[str, Any]:
        """Assess overall plan risk."""
        max_risk = primary_skill.risk
        risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

        for s in skill_chain:
            if risk_order.get(s.risk.value, 0) > risk_order.get(max_risk.value, 0):
                max_risk = s.risk

        return {
            "level": max_risk.value,
            "requires_approval": primary_skill.planning_guidance.requires_human_review,
            "warnings": recommendations["warnings"],
            "mitigation": "Run in staging first" if max_risk in [RiskLevel.HIGH, RiskLevel.CRITICAL] else "Standard review",
        }

    def _generate_checkpoints(self, skill: Skill) -> list[dict[str, Any]]:
        """Generate validation checkpoints for the plan."""
        checkpoints = []

        for criterion in skill.validation_guidance.success_criteria:
            checkpoints.append({
                "criterion": criterion,
                "type": "validation",
                "automated": "command" in criterion.lower() or "test" in criterion.lower(),
            })

        return checkpoints


def create_learning_engine(registry: SkillRegistry, evidence_store: EvidenceStore) -> SkillLearningEngine:
    """Factory for learning engine."""
    return SkillLearningEngine(registry, evidence_store)


def create_skill_aware_planner(registry: SkillRegistry, learning_engine: SkillLearningEngine) -> SkillAwarePlanner:
    """Factory for skill-aware planner."""
    return SkillAwarePlanner(registry, learning_engine)