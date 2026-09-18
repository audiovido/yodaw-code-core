"""Grok Architect: the routing/architecture brain.

Grok is deliberately *not* the default executor. It receives the goal,
real repository intelligence, the executors that actually exist on
this machine, and the routes the local gateway can serve — and returns
one strict, validated routing decision:

    task_type, summary, languages, frameworks, tools, executor,
    reason, plan[], acceptance[]

Non-negotiable rule: **malformed planner output never continues.**
Every backend's text goes through one parser, and the parsed object
must validate against ``PlanResult`` (which forbids unknown keys). A
failure raises ``PlannerError`` and the task ends ``FAILED`` with the
planner's raw output recorded as evidence — never a silent default
plan.

Backends, tried in order (configurable with ``KODGAR_PLANNER_BACKEND``):

1. ``grok-cli`` — the real Grok CLI in headless single-turn mode with
   ``--json-schema``, so the model is constrained by the schema itself.
2. ``9router`` — the local 9Router gateway (OpenAI-compatible), with a
   model chain so one dead route does not fail the plan.
3. ``local`` — the configured local model provider (Ollama by default),
   for fully offline/private operation.

Whichever backend served the plan is recorded on the task
(``planner_backend``), so the UI and the E2E report can state exactly
which engine decided.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from app.background.models import PlanResult, PlannerError
from app.background.repo_intel import (
    available_model_brief,
    collect_repo_intel,
)

DEFAULT_GROK_TIMEOUT = 240
DEFAULT_BACKEND_CHAIN = ("grok-cli", "9router", "local")

# Routes verified to answer on the local 9Router gateway, in
# preference order. Overridable with KODGAR_PLANNER_MODELS.
DEFAULT_ROUTER_MODELS = (
    "kgw/kilo-auto/free",
    "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
    "openrouter/google/gemma-4-31b-it:free",
)

SYSTEM_PROMPT = (
    "You are Kodgar's architect. You do not write code. You decide what "
    "a task requires and route it. Answer with a single JSON object and "
    "nothing else — no prose, no markdown fences."
)


@dataclass
class PlannerAttempt:
    backend: str
    detail: str
    raw: str = ""
    error: Optional[str] = None


class GrokArchitect:
    """Produces one validated ``PlanResult`` per task."""

    name = "grok"

    def __init__(
        self,
        backends: Optional[list[str]] = None,
        timeout: int = DEFAULT_GROK_TIMEOUT,
        repo_intel_fn=collect_repo_intel,
        model_brief_fn=available_model_brief,
    ):
        configured = os.environ.get("KODGAR_PLANNER_BACKEND", "").strip()
        if backends:
            self.backends = backends
        elif configured and configured != "auto":
            self.backends = [item.strip() for item in configured.split(",") if item.strip()]
        else:
            self.backends = list(DEFAULT_BACKEND_CHAIN)
        self.timeout = int(
            os.environ.get("KODGAR_PLANNER_TIMEOUT", str(timeout))
        )
        self.repo_intel_fn = repo_intel_fn
        self.model_brief_fn = model_brief_fn
        self.attempts: list[PlannerAttempt] = []
        self._router_key: str = ""

    # ----------------------------------------------------------- plan
    def plan(
        self,
        goal: str,
        repo: Optional[str],
        *,
        previous_evidence: Optional[list[dict]] = None,
        cancel_check=None,
    ) -> tuple[PlanResult, str, str]:
        """Return ``(plan, backend, reason)`` or raise ``PlannerError``.

        ``cancel_check`` is consulted between backends so a cancelled
        task stops before spending another model call.
        """
        self.attempts = []
        intel = self.repo_intel_fn(repo)
        prompt = self._build_prompt(
            goal, intel, repo, previous_evidence or []
        )
        schema = json.dumps(PlanResult.model_json_schema())

        errors: list[str] = []
        for backend in self.backends:
            if cancel_check is not None:
                cancel_check()
            try:
                if backend == "grok-cli":
                    raw = self._run_grok_cli(prompt, schema, repo)
                elif backend == "9router":
                    raw = self._run_9router(prompt)
                elif backend == "local":
                    raw = self._run_local(prompt)
                else:
                    errors.append(f"{backend}: unknown planner backend")
                    continue
            except PlannerError as exc:
                self.attempts.append(
                    PlannerAttempt(backend=backend, detail="", error=str(exc))
                )
                errors.append(f"{backend}: {exc}")
                continue
            except Exception as exc:
                self.attempts.append(
                    PlannerAttempt(
                        backend=backend,
                        detail="",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                errors.append(f"{backend}: {type(exc).__name__}: {exc}")
                continue

            try:
                plan = self._parse(raw, backend)
            except PlannerError as first:
                # One explicit, recorded repair round. A model that
                # returns the right shape but the wrong types gets
                # exactly one bounded chance to fix it against the
                # reported errors; if that also fails, the task is
                # BLOCKED. Nothing is ever defaulted silently.
                self.attempts.append(
                    PlannerAttempt(
                        backend=backend, detail="repair", raw=raw[:2000],
                        error=str(first),
                    )
                )
                repaired = self._repair(backend, prompt, raw, first, repo, schema)
                if repaired is None:
                    errors.append(f"{backend}: {first}")
                    continue
                raw = repaired
                try:
                    plan = self._parse(raw, backend)
                except PlannerError as second:
                    # Still unusable after a bounded repair: move on to
                    # the next backend rather than executing anything.
                    errors.append(f"{backend}: {second}")
                    continue

            self.attempts.append(
                PlannerAttempt(backend=backend, detail="", raw=raw[:2000])
            )
            return plan, backend, plan.reason

        raise PlannerError(
            "no planner backend produced a valid plan: " + "; ".join(errors[:4])
        )

    def _repair(
        self,
        backend: str,
        prompt: str,
        raw: str,
        error: PlannerError,
        repo: Optional[str],
        schema: str,
    ) -> Optional[str]:
        """One extra, strictly validated attempt with the errors named."""
        repair_prompt = (
            f"{prompt}\n\nYour previous answer was rejected by schema "
            f"validation.\nPrevious answer:\n{raw[:1500]}\n\nErrors:\n"
            f"{str(error)[:1500]}\n\nReturn ONE corrected JSON object. "
            "languages, frameworks, tools, plan, acceptance, target_files, "
            "expected_scope MUST be JSON arrays (use [] when empty, except "
            "languages/plan/acceptance which need at least one string). "
            "executor MUST be one of: claude-code, codex, grok-cli, "
            "kodgar-native. No prose, no markdown fences."
        )
        try:
            if backend == "grok-cli":
                return self._run_grok_cli(repair_prompt, schema, repo)
            if backend == "9router":
                return self._run_9router(repair_prompt)
            if backend == "local":
                return self._run_local(repair_prompt)
        except Exception as exc:
            self.attempts.append(
                PlannerAttempt(backend=backend, detail="repair", error=str(exc))
            )
        return None

    # -------------------------------------------------------- parsing
    def _parse(self, raw: str, backend: str) -> PlanResult:
        payload = extract_json_object(raw)
        if payload is None:
            raise PlannerError(
                f"{backend} returned no JSON object "
                f"(first 300 chars: {raw[:300]!r})"
            )
        if _is_error_envelope(payload):
            raise PlannerError(
                f"{backend} reported an error instead of a plan: "
                f"{_error_text(payload)[:300]}"
            )
        try:
            return PlanResult.model_validate(payload)
        except ValidationError as exc:
            raise PlannerError(
                f"{backend} planner output failed schema validation: "
                f"{exc.errors()[:4]}"
            ) from exc

    # ------------------------------------------------------- backends
    def _run_grok_cli(self, prompt: str, schema: str, repo: Optional[str]) -> str:
        binary = shutil.which("grok")
        if not binary:
            raise PlannerError("grok CLI not found on PATH")

        from app.workers.safe_subprocess import run

        cmd = [
            binary,
            "-p",
            prompt,
            "--json-schema",
            schema,
            "--output-format",
            "json",
            "--no-subagents",
            "--no-plan",
            "--max-turns",
            "1",
        ]
        if repo:
            cmd.extend(["--cwd", str(repo)])

        result = run(cmd, cwd=repo or str(Path.cwd()), timeout=self.timeout)
        if result.get("timed_out"):
            raise PlannerError(f"grok CLI exceeded {self.timeout}s")
        stdout = (result.get("stdout") or "").strip()
        if not stdout:
            raise PlannerError(
                f"grok CLI produced no output (rc={result.get('returncode')}, "
                f"stderr={((result.get('stderr') or '')[:200])!r})"
            )
        return stdout

    def _resolve_router_key(self, base: str) -> str:
        """Resolve a usable 9Router gateway key.

        The explicit ``NINEROUTER_API_KEY`` wins when it actually
        authenticates. Otherwise fall back to the product's own
        zero-touch provisioning (derive the local admin token, then
        reuse/mint a gateway key) so a missing env var never silently
        demotes planning to the local backend.
        """
        from app.llm.ninerouter import validate_gateway_key

        key = os.environ.get("NINEROUTER_API_KEY", "").strip()
        if key and validate_gateway_key(key, base, timeout=8):
            return key
        try:
            from app.llm.ninerouter import provision_gateway_key, resolve_admin

            _, token = resolve_admin(base)
            result = provision_gateway_key(base, token=token, existing_key=key)
            resolved = str(result.get("key") or "").strip()
            if resolved:
                return resolved
        except Exception:
            pass
        return key

    def _run_9router(self, prompt: str) -> str:
        import httpx

        from app.llm.ninerouter import (
            build_chat_request,
            list_models,
            normalize_base_url,
            parse_chat_response,
        )

        base = normalize_base_url(
            os.environ.get("NINEROUTER_BASE_URL", "http://127.0.0.1:20128")
        )
        key = self._router_key or self._resolve_router_key(base)
        self._router_key = key
        if not key:
            raise PlannerError(
                "no usable 9Router gateway key (NINEROUTER_API_KEY unset "
                "and zero-touch provisioning failed)"
            )

        configured = os.environ.get("KODGAR_PLANNER_MODELS", "").strip()
        candidates = (
            [item.strip() for item in configured.split(",") if item.strip()]
            if configured
            else list(DEFAULT_ROUTER_MODELS)
        )

        # Prefer routes the gateway actually advertises, but keep
        # configured candidates last so an offline inventory never
        # empties the chain.
        try:
            inventory = list_models(base, api_key=key, timeout=8)
            advertised = set(inventory.get("models") or []) | set(
                inventory.get("combos") or []
            )
            preferred = [model for model in candidates if model in advertised]
            tail = [model for model in candidates if model not in advertised]
            candidates = preferred + tail
        except Exception:
            pass

        failures: list[str] = []
        for model in candidates:
            url, payload, headers = build_chat_request(
                base, model, SYSTEM_PROMPT, prompt, api_key=key
            )
            payload["max_tokens"] = 1500
            try:
                response = httpx.post(
                    url, json=payload, headers=headers, timeout=self.timeout
                )
            except Exception as exc:
                failures.append(f"{model}: {type(exc).__name__}")
                continue
            if response.status_code != 200:
                failures.append(f"{model}: HTTP {response.status_code}")
                continue
            try:
                text = parse_chat_response(response.json())
            except Exception as exc:
                failures.append(f"{model}: {type(exc).__name__}")
                continue
            if not text.strip():
                failures.append(f"{model}: empty completion")
                continue
            return text

        raise PlannerError(
            "every 9Router planner route failed: " + "; ".join(failures[:4])
        )

    def _run_local(self, prompt: str) -> str:
        from app.llm.provider import LocalLLMProvider

        provider = LocalLLMProvider()
        text = provider.chat(SYSTEM_PROMPT, prompt)
        if not (text or "").strip():
            raise PlannerError("local provider returned an empty completion")
        return text

    # --------------------------------------------------------- prompt
    def _build_prompt(
        self,
        goal: str,
        intel: dict[str, Any],
        repo: Optional[str],
        previous_evidence: list[dict],
    ) -> str:
        models = self.model_brief_fn()
        evidence_brief = ""
        if previous_evidence:
            evidence_brief = (
                "\nPrevious task evidence (for context only):\n"
                + json.dumps(previous_evidence[-5:], default=str)[:3000]
                + "\n"
            )
        return (
            f"{SYSTEM_PROMPT}\n\n"
            "Choose the executor that fits the work. Available executors "
            "on this machine (an unavailable executor must not be chosen):\n"
            f"{json.dumps(intel.get('available_executors', []), indent=2)[:2000]}\n\n"
            "Executor guidance for V1 (defaults, not hard rules — justify "
            "any deviation in `reason`):\n"
            "- claude-code: large multi-file implementation, UI work\n"
            "- codex: deep debugging, repo analysis\n"
            "- grok-cli: architecture/research-heavy work\n"
            "- kodgar-native: simple/local/private tasks, deterministic "
            "file creation, anything that must work offline\n\n"
            "Repository intelligence:\n"
            f"{json.dumps(intel, default=str)[:6000]}\n"
            f"{evidence_brief}"
            f"\nGateway routes available (informational): {models[:20]}\n\n"
            f"Goal: {goal}\n\n"
            "Return exactly one JSON object with these keys: task_type, "
            "summary, languages, frameworks, tools, executor, reason, "
            "plan, acceptance.\n"
            "Every list key MUST be a JSON array of strings — never a "
            "single string. `languages`, `plan` and `acceptance` need at "
            "least one entry; use [] for unknown frameworks/tools.\n"
            'Shape example: {"task_type": "file_creation", "summary": '
            '"create the marker file", "languages": ["text"], '
            '"frameworks": [], "tools": ["git"], "executor": '
            '"kodgar-native", "reason": "deterministic single file", '
            '"plan": ["create the file"], "acceptance": '
            '["file exists with the exact content"]}\n'
            "Optional keys: target_files (relative paths), edits (list of "
            "{target_file, find, replace}; use find=\"\" to create a file "
            "with replace as its exact content), test_command, "
            "build_command, expected_scope.\n"
            "Rules:\n"
            "- executor MUST be one of: claude-code, codex, grok-cli, "
            "kodgar-native\n"
            "- acceptance must be objectively checkable\n"
            "- if the goal asks for an exact file or exact content, put it "
            "in `edits` so it can be produced deterministically\n"
            "- reply with JSON only\n"
        )


# ------------------------------------------------------------- helpers
def _is_error_envelope(payload: dict) -> bool:
    """CLIs signal failure in-band; that must never look like a plan."""
    if payload.get("type") == "error" or payload.get("is_error") is True:
        return True
    message = payload.get("message")
    return isinstance(message, str) and message.startswith("Internal error")


def _error_text(payload: dict) -> str:
    for key in ("message", "error", "result", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")
        if isinstance(value, dict):
            nested = value.get("message")
            if isinstance(nested, str) and nested.strip():
                return nested.strip().replace("\n", " ")
    return str(payload)[:300]


def extract_json_object(text: str) -> Optional[dict]:
    """Find the first JSON object in a model response.

    Tolerates the two failure modes seen in practice: fenced code
    blocks, and prose wrapped around the object. Returns ``None`` when
    no object parses — the caller must then fail loudly.
    """
    if not text:
        return None
    candidates: list[str] = []

    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("```")
        for chunk in body:
            cleaned = chunk.strip()
            if cleaned.startswith("{"):
                candidates.append(cleaned)
    candidates.append(stripped)

    start = stripped.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(stripped)):
            char = stripped[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(stripped[start : index + 1])
                    break
        start = stripped.find("{", start + 1)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            # Some CLIs wrap the answer: {"result": {...}} / {"plan": {...}}
            for key in ("result", "plan", "output", "response", "text"):
                inner = parsed.get(key)
                if isinstance(inner, dict) and "executor" in inner:
                    return inner
                if isinstance(inner, str):
                    nested = extract_json_object(inner)
                    if nested is not None:
                        return nested
            return parsed
    return None


def default_architect() -> GrokArchitect:
    return GrokArchitect()
