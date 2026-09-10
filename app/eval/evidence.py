"""
Evidence validator for benchmark evaluation.
"""
from typing import Dict, List, Tuple
from app.eval.models import BenchmarkCase


class EvidenceValidator:
    """Validate evidence quality from benchmark executions."""
    
    def validate(
        self,
        case: BenchmarkCase,
        evidence: Dict,
    ) -> Tuple[bool, List[str]]:
        """
        Validate evidence completeness and quality.
        
        Returns (is_valid, reasons).
        """
        reasons = []
        
        # For code-changing tasks
        if case.expected_success and case.intent.value not in ["review"]:
            required = self._validate_code_change_evidence(evidence)
            if not required[0]:
                return required
            reasons.extend(required[1])
        
        # For blocking tasks
        if not case.expected_success:
            blocked = self._validate_blocking_evidence(evidence)
            if not blocked[0]:
                return blocked
            reasons.extend(blocked[1])
        
        # For review tasks
        if case.intent.value == "review":
            review = self._validate_review_evidence(evidence)
            if not review[0]:
                return review
            reasons.extend(review[1])
        
        return True, reasons
    
    def _validate_code_change_evidence(
        self, evidence: Dict
    ) -> Tuple[bool, List[str]]:
        """Validate evidence for code-changing tasks."""
        reasons = []
        
        required_fields = [
            "mission_id",
            "goal",
            "changed_files",
            "diff",
            "validation_command",
            "validation_result",
            "final_status",
        ]
        
        missing = [f for f in required_fields if f not in evidence]
        if missing:
            return False, [f"missing required evidence fields: {missing}"]
        
        # Validate changed_files is not empty
        if not evidence.get("changed_files"):
            return False, ["changed_files is empty but success claimed"]
        
        # Validate commit presence if claimed
        if evidence.get("commit_sha"):
            if not evidence.get("commit_sha").strip():
                return False, ["commit_sha present but empty"]
            reasons.append("commit_sha present")
        
        # Validate validation result
        if evidence.get("validation_result") not in ["passed", "failed"]:
            return False, [f"invalid validation_result: {evidence.get('validation_result')}"]
        
        reasons.append("code change evidence complete")
        return True, reasons
    
    def _validate_blocking_evidence(
        self, evidence: Dict
    ) -> Tuple[bool, List[str]]:
        """Validate evidence for tasks that should block."""
        reasons = []
        
        # Should have blocking reason
        if not evidence.get("blocked_reason"):
            # Check if incorrectly claimed success
            if evidence.get("final_status") == "success":
                return False, ["false positive: claimed success on impossible task"]
            return False, ["missing blocked_reason"]
        
        # Should not have changed files
        if evidence.get("changed_files"):
            return False, ["incorrectly mutated code on blocked task"]
        
        reasons.append("blocking evidence valid")
        return True, reasons
    
    def _validate_review_evidence(
        self, evidence: Dict
    ) -> Tuple[bool, List[str]]:
        """Validate evidence for review tasks."""
        reasons = []
        
        # Review should have findings or explicit "no issues"
        if not evidence.get("review_findings") and not evidence.get("no_issues_found"):
            return False, ["missing review findings"]
        
        # Should not modify code unless fixing found issues
        if evidence.get("changed_files") and not evidence.get("fixes_applied"):
            return False, ["review should not modify code"]
        
        reasons.append("review evidence valid")
        return True, reasons
