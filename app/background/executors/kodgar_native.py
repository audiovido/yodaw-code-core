"""Kodgar native executor: the project's own edit engine.

This is the local/private executor. It reuses the machinery the
existing runtime already hardened instead of re-implementing any of
it:

- ``app.workers.edit_engine`` for every file mutation (byte-preserving
  writes, create/edit/delete, relaxed unique-match anchors, in-memory
  staging, exact restore on failure)
- ``app.workers.diff_guard`` for the credential scan on staged diffs
- ``app.llm.coder`` for the plan-driven mode

Two modes, both real:

1. **deterministic** — the planner produced explicit ``edits``; the
   engine stages them, writes them, and restores the baseline if any
   write fails, so a failed attempt never leaves a half-edited tree.
2. **plan-driven** — no explicit edits; the native LLM coder produces
   an edit plan for the worktree, exactly like the mission worker.

Either way the executor only claims "these filesystem writes
happened". Acceptance is the verifier's call.
"""

from __future__ import annotations

import time
from pathlib import Path

from app.background.executors.base import ExecutionContext, Executor
from app.background.models import ExecutionOutcome, TaskCancelled
from app.workers import edit_engine
from app.workers.diff_guard import scan as scan_secrets
from app.workers.worker_errors import MissionCancelled


class KodgarNativeExecutor(Executor):
    id = "kodgar-native"
    label = "Kodgar native"
    kind = "native"
    capabilities = (
        "deterministic-edits",
        "llm-edit-plan",
        "offline-capable",
        "private-local",
    )

    def available(self) -> tuple[bool, str]:
        # The native path has no external binary to probe: it is the
        # project's own engine and therefore always present.
        return True, "built-in edit engine (no external binary required)"

    def version(self) -> str:
        try:
            from app.product.version import __version__

            return str(__version__)
        except Exception:  # pragma: no cover - version is best effort
            return "native"

    # -------------------------------------------------------- execute
    def execute(self, ctx: ExecutionContext) -> ExecutionOutcome:
        started = time.monotonic()
        edits = [edit.model_dump() for edit in ctx.plan.edits]

        if edits:
            outcome = self._apply_deterministic(ctx, edits)
        else:
            outcome = self._apply_from_llm(ctx)

        outcome.output.setdefault(
            "duration_seconds", round(time.monotonic() - started, 2)
        )
        outcome.output.setdefault("mode", "deterministic" if edits else "llm")
        return outcome

    # ------------------------------------------------------- internals
    def _apply_deterministic(
        self, ctx: ExecutionContext, edits: list[dict]
    ) -> ExecutionOutcome:
        worktree = ctx.worktree
        # The planner already speaks the engine's edit dialect; wrap it
        # in the {"edits": [...]} envelope normalize_edits expects so
        # there is exactly one edit vocabulary in the product.
        normalized = edit_engine.normalize_edits({"edits": edits})
        if not normalized:
            return ExecutionOutcome(
                succeeded=False,
                summary="planner edits did not normalize",
                error={
                    "type": "InvalidEditPlan",
                    "message": "no valid edit survived normalization",
                },
            )

        (
            original_contents,
            staged_contents,
            prepared_edits,
            touched_files,
            error,
        ) = edit_engine.prepare_edits(worktree, normalized)

        if error is not None:
            return ExecutionOutcome(
                succeeded=False,
                summary="edit staging failed",
                error=dict(error.get("error") or {}),
                files_touched=[],
            )

        ctx.cancel_check()
        try:
            edit_engine.apply_edits(worktree, prepared_edits, staged_contents)
        except Exception as exc:
            edit_engine.restore_originals(
                worktree, original_contents, prepared_edits
            )
            return ExecutionOutcome(
                succeeded=False,
                summary="edit application failed; baseline restored",
                error={"type": type(exc).__name__, "message": str(exc)},
            )

        for path in touched_files:
            ctx.on_output("kodgar", f"wrote {path}")

        scan = scan_secrets(_staged_diff(worktree))
        if scan.get("blocking"):
            edit_engine.restore_originals(
                worktree, original_contents, prepared_edits
            )
            return ExecutionOutcome(
                succeeded=False,
                summary="credential scan blocked the change",
                error={
                    "type": "SecretBlocked",
                    "message": "staged changes contain detected credentials",
                    "blocking": scan["blocking"],
                },
            )

        return ExecutionOutcome(
            succeeded=True,
            exit_code=0,
            summary=f"applied {len(prepared_edits)} deterministic edit(s)",
            files_touched=list(touched_files or []),
            output={
                "edits": len(prepared_edits),
                "diff_scan": scan,
            },
        )

    def _apply_from_llm(self, ctx: ExecutionContext) -> ExecutionOutcome:
        from app.llm.coder import generate_edit_plan

        worktree = ctx.worktree
        ctx.on_output(
            "kodgar", "no explicit edits in plan; requesting an edit plan"
        )

        def cancel_check() -> None:
            try:
                ctx.cancel_check()
            except (TaskCancelled, MissionCancelled) as exc:
                raise MissionCancelled(str(exc)) from exc

        # The LLM edit-plan call is bounded by the task's remaining time
        # budget. Without this a slow provider could pin a worker thread
        # far past the task deadline (observed: 9+ minute hang).
        edit_plan_timeout = max(60.0, float(ctx.timeout_seconds or 300.0))

        def plan_deadline() -> None:
            cancel_check()
            if time.monotonic() - started > edit_plan_timeout:
                raise MissionCancelled(
                    f"native edit plan exceeded its {edit_plan_timeout:.0f}s budget"
                )

        try:
            plan = generate_edit_plan(
                ctx.task.goal,
                worktree,
                cancel_check=plan_deadline,
            )
        except MissionCancelled as exc:
            if ctx.task.cancel_requested:
                raise TaskCancelled(str(exc)) from exc
            return ExecutionOutcome(
                succeeded=False,
                summary="native edit plan cancelled",
                error={"type": "Cancelled", "message": str(exc)},
            )
        except Exception as exc:
            return ExecutionOutcome(
                succeeded=False,
                summary="native planner produced no usable edit plan",
                error={"type": type(exc).__name__, "message": str(exc)[:500]},
            )

        edits = edit_engine.normalize_edits(plan) if isinstance(plan, dict) else None
        if not edits:
            return ExecutionOutcome(
                succeeded=False,
                summary="LLM plan contained no applicable edits",
                error={
                    "type": "InvalidEditPlan",
                    "message": "plan normalization produced no edits",
                },
            )
        return self._apply_deterministic(ctx, edits)


def _staged_diff(worktree: Path) -> str:
    from app.workers.safe_subprocess import run

    result = run(["git", "diff"], cwd=str(worktree))
    return result.get("stdout", "")
