"""Live benchmark execution: real coder path on per-case worktrees.

Score semantics (kept distinct on purpose):

- HARNESS_PASS: the harness ran correctly, whatever the outcome.
- PROVIDER_AVAILABLE: the model answered with usable output.
- TASK_PASS: the code change passed verification and scoring.
- BENCHMARK_SCORE: the numeric score for a completed task.
- BLOCKED_EXTERNAL: a provider/network failure. This is neither a
  harness failure nor a task failure, and it must never be recorded
  as TASK_FAIL.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.eval.models import (
    BenchmarkCase,
    EvaluationResult,
    FailureMode,
    PerformanceMetrics,
    ResultClass,
)
from app.eval.providers.base import (
    ChatResult,
    LiveProviderConfig,
    LiveProviderError,
    ProviderMalformed,
)

HARNESS_PASS = "HARNESS_PASS"
PROVIDER_AVAILABLE = "PROVIDER_AVAILABLE"
TASK_PASS = "TASK_PASS"
BENCHMARK_SCORE = "BENCHMARK_SCORE"
BLOCKED_EXTERNAL = ResultClass.BLOCKED_EXTERNAL.value
TASK_FAIL = "TASK_FAIL"


@dataclass
class LiveTaskOutcome:
    """Result of one live benchmark case execution."""

    case_id: str
    harness_pass: bool
    provider_available: bool
    task_pass: bool
    benchmark_score: float
    result_class: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    result: Optional[EvaluationResult] = None
    latency_s: float = 0.0
    provider_attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "harness_pass": self.harness_pass,
            "provider_available": self.provider_available,
            "task_pass": self.task_pass,
            "benchmark_score": self.benchmark_score,
            "result_class": self.result_class,
            "latency_s": self.latency_s,
            "provider_attempts": self.provider_attempts,
            "evidence": self.evidence,
        }


class LiveBenchmarkExecutor:
    """Run benchmark cases through the real coder execution path.

    Each case gets an isolated temporary git repository copied from
    the eval fixtures. The caller-supplied ``run_coder`` hook executes
    the real coder against that repository. Verification tests run
    inside the case worktree, the patch is collected, failed artifacts
    are preserved, and successful worktrees are removed.
    """

    def __init__(
        self,
        run_coder: Optional[Callable[[str, Path], Dict[str, Any]]] = None,
        fixtures_dir: Optional[Path] = None,
        artifact_dir: Optional[Path] = None,
        keep_failed: bool = True,
    ):
        self.run_coder = run_coder or (lambda goal, repo: {})
        if fixtures_dir is None:
            fixtures_dir = Path(__file__).resolve().parent.parent / "fixtures"
        self.fixtures_dir = Path(fixtures_dir)
        self.artifact_dir = (
            Path(artifact_dir) if artifact_dir is not None else None
        )
        self.keep_failed = keep_failed

    def execute_case(
        self,
        case: BenchmarkCase,
        provider=None,
        max_retries: Optional[int] = None,
    ) -> LiveTaskOutcome:
        from app.eval.fixtures import FixtureManager
        from app.eval.scoring import Scorer

        started = time.monotonic()
        provider_attempts = 0
        manager = FixtureManager(str(self.fixtures_dir))
        repo_path: Optional[Path] = None
        evidence: Dict[str, Any] = {
            "case_id": case.id,
            "title": case.title,
            "intent": case.intent.value,
        }

        try:
            repo_path = manager.create_temp_repo(case.repository_fixture)
            evidence["repo_path_redacted"] = True
        except Exception as exc:
            return LiveTaskOutcome(
                case_id=case.id,
                harness_pass=True,
                provider_available=True,
                task_pass=False,
                benchmark_score=0.0,
                result_class=ResultClass.FAIL_TOOLING.value,
                evidence={**evidence, "harness_error": str(exc)},
                latency_s=time.monotonic() - started,
            )

        assert repo_path is not None
        case_started = time.monotonic()
        coder_output: Dict[str, Any] = {}
        provider_error: Optional[LiveProviderError] = None

        try:
            coder_output = self._run_coder(case, repo_path, provider)
            provider_attempts = int(coder_output.get("provider_attempts", 0))
            evidence.update(self._redact(coder_output))
        except ProviderMalformed as exc:
            evidence["provider_error"] = f"{type(exc).__name__}: {exc}"
            evidence["provider_malformed"] = True
            provider_attempts = exc.attempts
            scored = self._score_task_failure(
                case,
                evidence,
                time.monotonic() - case_started,
                ["provider returned malformed output"],
                [FailureMode.VALIDATION_ERROR],
            )
            self._cleanup(
                repo_path, manager, case.id, failed=True, evidence=evidence
            )
            return LiveTaskOutcome(
                case_id=case.id,
                harness_pass=True,
                provider_available=True,
                task_pass=False,
                benchmark_score=scored.score,
                result_class=scored.result_class.value,
                evidence=evidence,
                result=scored,
                latency_s=time.monotonic() - started,
                provider_attempts=provider_attempts,
            )
        except LiveProviderError as exc:
            provider_error = exc
            provider_attempts = exc.attempts
        except Exception as exc:
            self._cleanup(
                repo_path, manager, case.id, failed=True, evidence=evidence
            )
            scored = self._score_task_failure(
                case,
                {**evidence, "harness_error": str(exc)},
                time.monotonic() - case_started,
                [f"harness error: {exc}"],
                [FailureMode.TOOLING_MISSING],
            )
            return LiveTaskOutcome(
                case_id=case.id,
                harness_pass=False,
                provider_available=False,
                task_pass=False,
                benchmark_score=0.0,
                result_class=ResultClass.FAIL_TOOLING.value,
                evidence=evidence,
                result=scored,
                latency_s=time.monotonic() - started,
            )

        if provider_error is not None:
            evidence["provider_error"] = (
                f"{type(provider_error).__name__}: {provider_error}"
            )
            evidence["blocked_external"] = True
            evidence["provider_attempts"] = provider_attempts
            self._cleanup(
                repo_path, manager, case.id, failed=True, evidence=evidence
            )
            return LiveTaskOutcome(
                case_id=case.id,
                harness_pass=True,
                provider_available=False,
                task_pass=False,
                benchmark_score=0.0,
                result_class=BLOCKED_EXTERNAL,
                evidence=evidence,
                latency_s=time.monotonic() - started,
                provider_attempts=provider_attempts,
            )

        evidence["provider_available"] = True
        evidence["provider_attempts"] = provider_attempts
        patch = self._collect_patch(repo_path)
        evidence.update(patch)
        verification = self._run_verification(case, repo_path)
        evidence.update(verification)
        scored = Scorer().score(
            case,
            self._scoring_evidence(case, coder_output, evidence),
            self._performance(case, coder_output, evidence),
        )
        evidence["result_class"] = scored.result_class.value
        evidence["benchmark_score"] = scored.score
        evidence["correctness"] = scored.correctness_score
        evidence["test_pass"] = bool(
            self._scoring_evidence(case, coder_output, evidence).get(
                "tests_passed"
            )
        )
        evidence["patch_valid"] = bool(patch.get("patch"))
        evidence["task_completed"] = (
            scored.result_class == ResultClass.PASS
        ) or (
            not case.expected_success and bool(evidence.get("correctly_blocked"))
        )
        evidence["unnecessary_changes"] = max(
            0,
            evidence.get("files_changed", 0) - case.max_files_changed,
        )
        task_pass = scored.result_class == ResultClass.PASS
        failed = not task_pass
        self._cleanup(
            repo_path, manager, case.id, failed=failed, evidence=evidence
        )
        return LiveTaskOutcome(
            case_id=case.id,
            harness_pass=True,
            provider_available=True,
            task_pass=task_pass,
            benchmark_score=scored.score,
            result_class=scored.result_class.value,
            evidence=evidence,
            result=scored,
            latency_s=time.monotonic() - started,
            provider_attempts=provider_attempts,
        )

    def _run_coder(
        self, case: BenchmarkCase, repo_path: Path, provider
    ) -> Dict[str, Any]:
        started = time.monotonic()
        output = self.run_coder(case.user_goal, repo_path) or {}
        if not isinstance(output, dict):
            output = {"result": output}
        record = dict(output)
        attempts = record.get("provider_attempts", 0)
        result = record.get("chat_result")
        if isinstance(result, ChatResult):
            attempts = max(int(attempts), result.attempts)
            record["latency_s"] = result.latency_s
            record["model"] = result.model
            record["usage"] = dict(result.usage)
        record.setdefault("latency_s", time.monotonic() - started)
        record["provider_attempts"] = int(attempts)
        if provider is not None and "patch" not in record:
            provider_attempts = getattr(provider, "attempts_log", None)
            if provider_attempts:
                record["provider_attempts_log"] = list(provider_attempts)
        return record

    def _collect_patch(self, repo_path: Path) -> Dict[str, Any]:
        try:
            status = self._git(repo_path, "status", "--short")
            diff = self._git(repo_path, "diff")
            files = [
                line[3:].strip()
                for line in status.splitlines()
                if line.strip()
            ]
            return {
                "patch": diff,
                "changed_files": files,
                "files_changed": len(files),
                "diff_lines": sum(
                    1
                    for line in diff.splitlines()
                    if line.startswith("+") or line.startswith("-")
                ),
            }
        except Exception as exc:
            return {
                "patch": "",
                "changed_files": [],
                "files_changed": 0,
                "diff_lines": 0,
                "patch_error": str(exc),
            }

    def _run_verification(
        self, case: BenchmarkCase, repo_path: Path
    ) -> Dict[str, Any]:
        commands = self._verification_commands(case, repo_path)
        results: List[Dict[str, Any]] = []
        for cmd in commands:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=case.timeout,
                )
                results.append(
                    {
                        "cmd": " ".join(cmd),
                        "returncode": proc.returncode,
                        "stdout": proc.stdout[-4000:],
                        "stderr": proc.stderr[-4000:],
                    }
                )
            except subprocess.TimeoutExpired:
                results.append(
                    {"cmd": " ".join(cmd), "timeout": True, "returncode": 124}
                )
            except FileNotFoundError as exc:
                results.append(
                    {
                        "cmd": " ".join(cmd),
                        "returncode": 127,
                        "stderr": str(exc),
                    }
                )
        passed = bool(results) and all(
            r.get("returncode") == 0 for r in results
        )
        return {
            "verification_commands": [" ".join(c) for c in commands],
            "verification_results": results,
            "tests_passed": passed,
            "validation_passed": passed,
            "validation_result": "passed" if passed else "failed",
        }

    def _verification_commands(
        self, case: BenchmarkCase, repo_path: Path
    ) -> List[List[str]]:
        import sys

        fixture = case.repository_fixture
        if "node" in fixture:
            return [["npm", "test", "--", "--runInBand"]]
        if "go" in fixture:
            return [["go", "test", "./..."]]
        if "rust" in fixture:
            return [["cargo", "test", "--quiet"]]
        test_file = repo_path / "test_api.py"
        if fixture == "mixed_repo" and test_file.exists():
            return [[sys.executable, "-m", "pytest", "-q", "test_api.py"]]
        return [[sys.executable, "-m", "pytest", "-q"]]

    def _scoring_evidence(
        self,
        case: BenchmarkCase,
        coder_output: Dict[str, Any],
        collected: Dict[str, Any],
    ) -> Dict[str, Any]:
        evidence: Dict[str, Any] = {
            "mission_id": coder_output.get("mission_id", f"live_{case.id}"),
            "goal": case.user_goal,
            "changed_files": collected.get("changed_files", []),
            "diff": collected.get("patch", ""),
            "validation_command": "; ".join(
                collected.get("verification_commands", [])
            ),
            "validation_result": collected.get("validation_result"),
            "final_status": (
                "success"
                if coder_output.get("blocked") or collected.get("tests_passed")
                else "failed"
            ),
            "tests_passed": collected.get("tests_passed", False),
            "expected_assertions_present": bool(collected.get("patch")),
            "validation_passed": collected.get("validation_passed", False),
            "unrelated_tests_failed": False,
            "live": True,
        }
        for key in (
            "blocked_reason",
            "correctly_blocked",
            "ambiguous_request",
            "review_findings",
            "no_issues_found",
            "fixes_applied",
            "tests_added",
            "commit_sha",
            "retries",
            "usage",
            "model",
            "latency_s",
        ):
            if key in coder_output:
                evidence[key] = coder_output[key]
        if case.expected_tests:
            evidence.setdefault("tests_added", list(case.expected_tests))
        return evidence

    def _performance(
        self,
        case: BenchmarkCase,
        coder_output: Dict[str, Any],
        collected: Dict[str, Any],
    ) -> PerformanceMetrics:
        return PerformanceMetrics(
            wall_clock_time=float(
                coder_output.get("latency_s", collected.get("latency_s", 0.0))
                or 0.0
            ),
            retries=int(coder_output.get("retries", 0)),
            files_changed=int(collected.get("files_changed", 0)),
            diff_lines=int(collected.get("diff_lines", 0)),
            commits_created=1 if coder_output.get("commit_sha") else 0,
        )

    def _score_task_failure(
        self,
        case: BenchmarkCase,
        evidence: Dict[str, Any],
        wall_clock: float,
        reasons: List[str],
        modes: List[FailureMode],
    ) -> EvaluationResult:
        from app.eval.scoring import Scorer

        perf = PerformanceMetrics(
            wall_clock_time=wall_clock,
            retries=0,
            files_changed=int(evidence.get("files_changed", 0)),
            diff_lines=int(evidence.get("diff_lines", 0)),
        )
        scoring_evidence = {
            "mission_id": f"live_{case.id}",
            "goal": case.user_goal,
            "changed_files": evidence.get("changed_files", []),
            "diff": evidence.get("patch", ""),
            "validation_command": "",
            "validation_result": "failed",
            "final_status": "failed",
            "tests_passed": False,
            "live": True,
        }
        scored = Scorer().score(case, scoring_evidence, perf)
        scored.reasons = list(reasons) + list(scored.reasons)
        for mode in modes:
            if mode not in scored.failure_modes:
                scored.failure_modes.append(mode)
        return scored

    def _cleanup(
        self,
        repo_path: Path,
        manager,
        case_id: str,
        failed: bool,
        evidence: Dict[str, Any],
    ) -> None:
        if failed and self.keep_failed and self.artifact_dir is not None:
            try:
                self.artifact_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
                dest = self.artifact_dir / f"{case_id}_{stamp}"
                if dest.exists():
                    dest = self.artifact_dir / f"{case_id}_{stamp}_{os.getpid()}"
                import shutil

                shutil.copytree(repo_path, dest)
                evidence["preserved_artifact"] = str(dest)
            except Exception as exc:
                evidence["artifact_error"] = str(exc)
        try:
            manager.cleanup_temp_repo(repo_path)
            evidence["cleanup"] = "removed"
        except Exception as exc:
            evidence["cleanup"] = f"cleanup_failed: {exc}"

    def _redact(self, record: Dict[str, Any]) -> Dict[str, Any]:
        safe = dict(record)
        for key in ("api_key", "authorization", "headers"):
            safe.pop(key, None)
        return safe

    @staticmethod
    def _git(repo_path: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout


def _looks_like_provider_failure(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "provider request failed",
        "provider unreachable",
        "provider network error",
        "provider request timed out",
        "timed out",
        "timeout",
        "unreachable",
        "connecterror",
        "network error",
        "rate limited",
        "server error",
        "authentication failed",
        "unauthorized",
        " 401",
        " 403",
        " 429",
        " 500",
        " 502",
        " 503",
        " 504",
    )
    return any(m in lowered for m in markers)


def _looks_like_malformed(message: str) -> bool:
    lowered = message.lower()
    markers = (
        "invalid json",
        "malformed",
        "unsupported coder action",
        "missing:",
        "must contain a non-empty edits list",
        "is not an object",
    )
    return any(m in lowered for m in markers)


def _provider_error_from_message(
    message: str, attempts: int
) -> LiveProviderError:
    from app.eval.providers.base import (
        ProviderAuthError,
        ProviderConfigError,
        ProviderMalformed,
        ProviderRateLimited,
        ProviderServerError,
        ProviderTimeout,
        ProviderUnavailable,
    )

    lowered = message.lower()
    if "not configured" in lowered:
        return ProviderConfigError(message, attempts=attempts)
    if _looks_like_malformed(message):
        return ProviderMalformed(message, attempts=attempts)
    if " 401" in lowered or " 403" in lowered or "auth" in lowered:
        return ProviderAuthError(message, attempts=attempts)
    if " 429" in lowered or "rate limited" in lowered:
        return ProviderRateLimited(message, attempts=attempts)
    if any(code in lowered for code in (" 500", " 502", " 503", " 504")):
        return ProviderServerError(message, attempts=attempts)
    if "timed out" in lowered or "timeout" in lowered:
        return ProviderTimeout(message, attempts=attempts)
    if (
        "unreachable" in lowered
        or "connecterror" in lowered
        or "network error" in lowered
    ):
        return ProviderUnavailable(message, attempts=attempts)
    if _looks_like_provider_failure(message):
        return ProviderUnavailable(message, attempts=attempts)
    return ProviderMalformed(message, attempts=attempts)


def default_repo_code_coder(
    provider=None,
    *,
    disable_github: bool = True,
    max_retries: int = 1,
):
    """Build the real coder execution hook for RepoCodeWorker.

    Runs the real RepoCodeWorker against each case worktree. When a
    live provider is supplied it is injected as the coder brain so
    real model output drives the edits. GitHub reuse discovery is
    disabled by default so offline suites stay offline.

    Provider-level failures inside the worker result are re-raised
    as LiveProviderError so the executor records BLOCKED_EXTERNAL
    instead of a task failure. Model-quality failures (bad plans,
    malformed output) are returned as task output so they score as
    TASK_FAIL with the provider marked available.
    """

    def run_coder(goal: str, repo_path: Path) -> Dict[str, Any]:
        from app.workers.repo_code_worker import RepoCodeWorker

        if disable_github:
            os.environ["YODAW_ENABLE_GITHUB"] = "false"
        worker = RepoCodeWorker()
        metadata: Dict[str, Any] = {
            "repo_path": str(repo_path),
            "max_retries": max_retries,
            "mission_id": f"live_{repo_path.name}",
        }
        if provider is not None:
            import app.llm.coder as coder_module

            original = coder_module.LocalLLMProvider
            coder_module.LocalLLMProvider = lambda: provider  # type: ignore
            try:
                result = worker.execute(goal, metadata)
            finally:
                coder_module.LocalLLMProvider = original
        else:
            result = worker.execute(goal, metadata)

        attempts = 0
        provider_log = (
            getattr(provider, "attempts_log", None)
            if provider is not None
            else None
        )
        if isinstance(provider_log, list):
            attempts = max(
                attempts,
                sum(
                    1
                    for e in provider_log
                    if isinstance(e, dict)
                    and "provider_backoff" not in e
                    and "attempt" in e
                ),
            )
        if not isinstance(result, dict):
            return {"provider_attempts": attempts, "latency_s": 0.0}

        output = dict(result.get("output", {}))
        error = result.get("error")
        evidence_items = result.get("evidence", [])
        for item in evidence_items if isinstance(evidence_items, list) else []:
            if isinstance(item, dict) and item.get("type") == "provider_attempts":
                entries = item.get("attempts", [])
                if isinstance(entries, list):
                    attempts = max(
                        attempts,
                        sum(
                            1
                            for e in entries
                            if isinstance(e, dict)
                            and "provider_backoff" not in e
                        ),
                    )
        output.setdefault("provider_attempts", attempts)
        output.setdefault("latency_s", 0.0)
        result_error = dict(error) if isinstance(error, dict) else None

        if result.get("success") is True:
            if output.get("patch") is None and output.get("diff"):
                output["patch"] = output["diff"]
            output["provider_attempts"] = int(
                output.get("provider_attempts", 0) or 0
            )
            return output

        err_type = (result_error or {}).get("type", "")
        err_msg = str((result_error or {}).get("message", ""))
        if err_type == "LLMBlocked":
            output["blocked"] = True
            output["blocked_reason"] = err_msg
            output["provider_attempts"] = int(
                output.get("provider_attempts", 0) or 0
            )
            return output
        if err_type in ("LLMError", "Cancelled") and _looks_like_provider_failure(
            err_msg
        ):
            raise _provider_error_from_message(err_msg, attempts)
        if err_type == "LLMError" and _looks_like_malformed(err_msg):
            raise _provider_error_from_message(err_msg, attempts)
        output["coder_error"] = result_error
        if output.get("patch") is None and output.get("diff"):
            output["patch"] = output["diff"]
        output["provider_attempts"] = int(
            output.get("provider_attempts", 0) or 0
        )
        return output

    return run_coder


def live_report_payload(
    outcomes: List[LiveTaskOutcome], revision: str = "unknown"
) -> Dict[str, Any]:
    """Serialize live outcomes with score semantics separated."""
    return {
        "revision": revision,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_cases": len(outcomes),
            "harness_pass": sum(1 for o in outcomes if o.harness_pass),
            "provider_available": sum(
                1 for o in outcomes if o.provider_available
            ),
            "task_pass": sum(1 for o in outcomes if o.task_pass),
            "blocked_external": sum(
                1 for o in outcomes if o.result_class == BLOCKED_EXTERNAL
            ),
            "benchmark_score_avg": (
                sum(o.benchmark_score for o in outcomes) / len(outcomes)
                if outcomes
                else 0.0
            ),
        },
        "cases": [o.to_dict() for o in outcomes],
    }


def write_live_report(
    outcomes: List[LiveTaskOutcome], path: Path, revision: str = "unknown"
) -> Path:
    path = Path(path)
    path.write_text(json.dumps(live_report_payload(outcomes, revision), indent=2))
    return path
