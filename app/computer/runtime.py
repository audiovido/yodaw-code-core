"""ComputerRuntime: the OBSERVE -> UNDERSTAND -> PLAN -> ACT -> VERIFY -> RECOVER loop."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.computer.actions import (
    ActionRequest,
    ActionResult,
    ActionType,
    ErrorClass,
    VerificationState,
    now_ts,
)
from app.computer.backend import ComputerBackend
from app.computer.evidence import EvidenceStore
from app.computer.geometry import (
    CoordinateError,
    ScreenGeometry,
    resolve_point,
    validate_point,
    validate_region,
)
from app.computer.policy import ApprovalRequired, check_approval
from app.computer.recovery import plan_recovery
from app.computer.secrets import SecretValue, sanitize, sanitize_message
from app.computer.session import ComputerSession, SessionStatus
from app.computer.timing import TimingConfig
from app.computer.verification import (
    Observation,
    VerifyPredicate,
    verify_action,
)

logger = logging.getLogger("yodaw.computer")

_MOUSE_ACTIONS = frozenset(
    {
        ActionType.MOVE_MOUSE,
        ActionType.CLICK,
        ActionType.DOUBLE_CLICK,
        ActionType.RIGHT_CLICK,
        ActionType.MOUSE_DOWN,
        ActionType.MOUSE_UP,
        ActionType.SCROLL,
    }
)

_RETRYABLE_UNVERIFIED = frozenset(
    {
        ActionType.CLICK,
        ActionType.DOUBLE_CLICK,
        ActionType.TYPE_TEXT,
        ActionType.DRAG,
        ActionType.PRESS_KEY,
        ActionType.HOTKEY,
    }
)


@dataclass
class RuntimeConfig:
    timing: TimingConfig = field(default_factory=TimingConfig)
    dry_run: bool = False
    evidence_dir: str | None = None
    default_timeout_seconds: float = 30.0


def _error_result(
    request: ActionRequest,
    params_sanitized: dict,
    error_class: ErrorClass,
    message: str,
    verification: VerificationState,
    duration_ms: float,
) -> ActionResult:
    return ActionResult(
        action_id=request.action_id,
        action_type=request.action_type,
        timestamp=now_ts(),
        requested_params=params_sanitized,
        execution_result={"ok": False},
        duration_ms=duration_ms,
        verification=verification,
        evidence_ref=None,
        error_class=error_class,
        error_message=message,
        risk=request.risk,
        dry_run=request.dry_run,
    )


class ComputerRuntime:
    """Provider-agnostic desktop control with verified actions.

    All platform specifics live in the injected ComputerBackend;
    this class owns validation, safety gates, secret hygiene,
    verification, and bounded recovery.
    """

    def __init__(
        self,
        backend: ComputerBackend,
        config: RuntimeConfig | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.backend = backend
        self.config = config or RuntimeConfig()
        self.evidence = EvidenceStore(artifact_dir=self.config.evidence_dir)
        self.session = ComputerSession(
            platform=backend.platform(),
            geometry=backend.screen_geometry(),
            dry_run=self.config.dry_run,
            status=SessionStatus.READY,
        )
        try:
            active = backend.active_window()
            self.session.active_app = active.app_name
            self.session.active_window = active.window_title
        except Exception:
            pass
        self._on_event = on_event
        self._last_observation: Observation = Observation()

    # --------------------------------------------------
    # OBSERVE
    # --------------------------------------------------
    def observe(
        self,
        region: tuple[float, float, float, float] | None = None,
        active_window_only: bool = False,
    ) -> dict:
        """Capture a screenshot plus active-window context for planners."""
        shot = self.backend.screenshot(
            region=region, active_window_only=active_window_only
        )
        record = self.evidence.record(
            self.session.last_action_id or "observe",
            "screenshot",
            sanitize(
                {
                    "width": shot.width,
                    "height": shot.height,
                    "scale_factor": shot.scale_factor,
                    "region": shot.region,
                    "artifact_id": shot.artifact_id,
                }
            ),
        )
        artifact_path = self.evidence.save_screenshot_artifact(
            shot.png_bytes, record.evidence_id
        )
        try:
            active = self.backend.active_window()
            self.session.active_app = active.app_name
            self.session.active_window = active.window_title
        except Exception:
            pass
        return {
            "width": shot.width,
            "height": shot.height,
            "scale_factor": shot.scale_factor,
            "timestamp": shot.timestamp,
            "active_app": shot.active_app,
            "active_window": shot.active_window,
            "region": shot.region,
            "artifact_id": shot.artifact_id,
            "artifact_path": artifact_path,
            "evidence_id": record.evidence_id,
            "backend_note": shot.backend_note,
        }

    # --------------------------------------------------
    # ACT
    # --------------------------------------------------
    def act(
        self,
        action: ActionRequest | None = None,
        action_type: ActionType | str | None = None,
        params: dict | None = None,
        approved: bool = False,
        verify_predicate: VerifyPredicate | None = None,
        verify_context: dict | None = None,
    ) -> ActionResult:
        """Validate, execute, verify, and recover one structured action."""
        started = time.monotonic()
        request = self._coerce_request(action, action_type, params)
        safe_params = sanitize(request.params)

        if self.session.status == SessionStatus.CLOSED:
            return _error_result(
                request,
                dict(safe_params),
                ErrorClass.VALIDATION,
                "session is closed",
                VerificationState.FAILED,
                0.0,
            )

        secrets = self._collect_secrets(request.params)

        # Safety gate: HIGH-risk actions need explicit approval.
        try:
            check_approval(request, approved=approved)
        except ApprovalRequired as exc:
            self._emit("approval_required", {"action_id": request.action_id})
            return _error_result(
                request,
                dict(safe_params),
                ErrorClass.APPROVAL_REQUIRED,
                str(exc),
                VerificationState.FAILED,
                self._elapsed_ms(started),
            )

        # Refresh geometry; a changed layout invalidates old coordinates.
        try:
            fresh = self.backend.screen_geometry()
        except Exception as exc:
            return self._backend_error(
                request, safe_params, started, exc, secrets
            )
        if not self.session.geometry.same_layout(fresh):
            self.session.geometry = fresh
            recovery = plan_recovery(screen_changed=True)
            self._emit("screen_changed", {"recovery": recovery.action.value})

        # UNDERSTAND + PLAN: validate and resolve the target.
        try:
            plan = self._plan(request, self.session.geometry)
        except CoordinateError as exc:
            return _error_result(
                request,
                dict(safe_params),
                ErrorClass.VALIDATION,
                str(exc),
                VerificationState.FAILED,
                self._elapsed_ms(started),
            )

        if request.dry_run or self.config.dry_run:
            return self._dry_run_result(request, safe_params, plan, started)

        # ACT with screenshot-before/after observation.
        before = self._capture_state()
        try:
            exec_result = self._dispatch(request, plan)
        except PermissionError as exc:
            return self._blocked_result(
                request, safe_params, started, exc, secrets
            )
        except TimeoutError as exc:
            return _error_result(
                request,
                dict(safe_params),
                ErrorClass.TIMEOUT,
                sanitize_message(str(exc), secrets),
                VerificationState.FAILED,
                self._elapsed_ms(started),
            )
        except Exception as exc:
            return self._backend_error(
                request, safe_params, started, exc, secrets
            )

        if not exec_result.ok:
            return self._map_backend_failure(
                request, safe_params, started, exec_result, secrets
            )

        self._settle()
        after = self._capture_state()
        self._last_observation = Observation(
            pixels_before=before[0],
            pixels_after=after[0],
            app_before=before[1],
            app_after=after[1],
            window_before=before[2],
            window_after=after[2],
        )
        state = verify_action(
            request,
            self.backend,
            self._last_observation,
            True,
            predicate=verify_predicate,
            predicate_context=verify_context,
        )

        # RECOVER: one bounded retry for safe, retryable actions.
        attempts = 0
        while (
            state == VerificationState.UNVERIFIED
            and attempts < 1
            and request.action_type in _RETRYABLE_UNVERIFIED
            and request.risk.value != "HIGH"
        ):
            recovery = plan_recovery(
                no_observable_change=True, attempts_so_far=attempts
            )
            if not recovery.retry_allowed:
                break
            self._emit(
                "retry",
                {"action_id": request.action_id, "attempt": attempts + 1},
            )
            attempts += 1
            before = self._capture_state()
            try:
                exec_result = self._dispatch(request, plan)
            except Exception as exc:
                return self._backend_error(
                    request, safe_params, started, exc, secrets
                )
            if not exec_result.ok:
                return self._map_backend_failure(
                    request, safe_params, started, exec_result, secrets
                )
            self._settle()
            after = self._capture_state()
            self._last_observation = Observation(
                pixels_before=before[0],
                pixels_after=after[0],
                app_before=before[1],
                app_after=after[1],
                window_before=before[2],
                window_after=after[2],
            )
            state = verify_action(
                request,
                self.backend,
                self._last_observation,
                True,
                predicate=verify_predicate,
                predicate_context=verify_context,
            )

        if state == VerificationState.FAILED:
            recovery = plan_recovery(backend_error=True)
            self._emit("verify_failed", {"action_id": request.action_id,
                                         "recovery": recovery.action.value})

        record = self.evidence.record(
            request.action_id,
            "action",
            {
                **dict(safe_params),
                "verification": state.value,
                "attempts": attempts + 1,
            },
        )
        self.session.touch(request.action_id)
        self._refresh_active_window()
        return ActionResult(
            action_id=request.action_id,
            action_type=request.action_type,
            timestamp=now_ts(),
            requested_params=dict(safe_params),
            execution_result=self._safe_exec_summary(exec_result, secrets),
            duration_ms=self._elapsed_ms(started),
            verification=state,
            evidence_ref=record.evidence_id,
            error_class=ErrorClass.NONE,
            error_message=None,
            risk=request.risk,
            dry_run=False,
        )

    # --------------------------------------------------
    # VERIFY + STATUS (integration surface for planners)
    # --------------------------------------------------
    def verify(
        self,
        predicate: VerifyPredicate,
        context: dict | None = None,
    ) -> VerificationState:
        """Evaluate a caller-provided predicate against last observation."""
        try:
            passed = bool(predicate(dict(context or {})))
        except Exception:
            return VerificationState.FAILED
        return (
            VerificationState.VERIFIED if passed else VerificationState.FAILED
        )

    def session_status(self) -> dict:
        """Current session snapshot for planners and operators."""
        return self.session.describe()

    def pause(self) -> None:
        if self.session.status == SessionStatus.ACTIVE:
            self.session.status = SessionStatus.PAUSED

    def resume(self) -> None:
        if self.session.status == SessionStatus.PAUSED:
            self.session.status = SessionStatus.ACTIVE

    def close(self) -> None:
        self.session.status = SessionStatus.CLOSED

    # --------------------------------------------------
    # internals
    # --------------------------------------------------
    def _coerce_request(
        self,
        action: ActionRequest | None,
        action_type: ActionType | str | None,
        params: dict | None,
    ) -> ActionRequest:
        if action is not None:
            return action
        if action_type is None:
            raise ValueError("act() needs an action or an action_type")
        if isinstance(action_type, str):
            action_type = ActionType(action_type)
        return ActionRequest(
            action_type=action_type,
            params=dict(params or {}),
            timeout_seconds=self.config.default_timeout_seconds,
        )

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (time.monotonic() - started) * 1000.0

    def _emit(self, event: str, data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event, data)
            except Exception:
                pass
        logger.debug("computer %s %s", event, sanitize(data))

    @staticmethod
    def _collect_secrets(params: dict) -> list[str]:
        found: list[str] = []
        stack = [params]
        while stack:
            item = stack.pop()
            if isinstance(item, SecretValue):
                text = item.reveal()
                if text:
                    found.append(text)
            elif isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, (list, tuple)):
                stack.extend(item)
        return found

    @staticmethod
    def _safe_exec_summary(exec_result: Any, secrets: list[str]) -> dict:
        data = dict(getattr(exec_result, "data", {}) or {})
        summary = sanitize(data)
        assert isinstance(summary, dict)
        if secrets:
            # Belt and suspenders: backend echoes must never leak secrets.
            for key, value in list(summary.items()):
                if isinstance(value, str):
                    summary[key] = sanitize_message(value, secrets)
        return summary

    def _settle(self) -> None:
        delay = self.config.timing.action_settle_ms / 1000.0
        if delay > 0:
            time.sleep(delay)

    def _refresh_active_window(self) -> None:
        try:
            active = self.backend.active_window()
            self.session.active_app = active.app_name
            self.session.active_window = active.window_title
        except Exception:
            pass

    def _capture_state(self) -> tuple[bytes, str, str]:
        pixels = b""
        getter = getattr(self.backend, "last_pixels", None)
        if callable(getter):
            try:
                pixels = bytes(getter() or b"")
            except Exception:
                pixels = b""
        try:
            active = self.backend.active_window()
            return (pixels, active.app_name, active.window_title)
        except Exception:
            return (pixels, "", "")

    def _plan(self, request: ActionRequest, geometry: ScreenGeometry) -> dict:
        """Validate params and resolve coordinates. Pure: no side effects."""
        params = request.params
        needs_point = request.action_type in _MOUSE_ACTIONS
        plan: dict[str, Any] = {}
        if request.action_type == ActionType.DRAG:
            plan["from"] = self._resolve_endpoint(params, geometry, "from")
            plan["to"] = self._resolve_endpoint(params, geometry, "to")
            return plan
        if needs_point:
            plan["point"] = self._resolve_endpoint(params, geometry, "")
            return plan
        if request.action_type == ActionType.SCREENSHOT:
            region = params.get("region")
            if region is not None:
                x, y, width, height = (float(v) for v in region)
                validate_region(geometry, x, y, width, height)
                plan["region"] = (x, y, width, height)
            return plan
        if request.action_type == ActionType.WAIT:
            seconds = float(params.get("seconds", params.get("ms", 500)))
            if "ms" in params and "seconds" not in params:
                seconds = float(params["ms"]) / 1000.0
            if seconds < 0 or seconds > request.timeout_seconds:
                raise CoordinateError(
                    f"wait {seconds}s outside timeout "
                    f"{request.timeout_seconds}s"
                )
            plan["seconds"] = seconds
            return plan
        if request.action_type == ActionType.TYPE_TEXT:
            if "secret" in params and "text" in params:
                raise CoordinateError(
                    "type_text takes either text or secret, not both"
                )
            if "secret" in params and not isinstance(
                params["secret"], SecretValue
            ):
                raise CoordinateError("secret must be a SecretValue")
            if "text" not in params and "secret" not in params:
                raise CoordinateError("type_text needs text or secret")
            return plan
        if request.action_type == ActionType.HOTKEY:
            keys = params.get("keys")
            if not isinstance(keys, list) or not keys:
                raise CoordinateError("hotkey needs a non-empty keys list")
            return plan
        if request.action_type == ActionType.PRESS_KEY:
            if not params.get("key"):
                raise CoordinateError("press_key needs a key name")
            return plan
        return plan

    def _resolve_endpoint(
        self, params: dict, geometry: ScreenGeometry, prefix: str
    ) -> tuple[float, float]:
        suffix = f"_{prefix}" if prefix else ""
        alt = f"{prefix}_" if prefix else ""
        x = params.get(f"x{suffix}", params.get(f"{alt}x"))
        y = params.get(f"y{suffix}", params.get(f"{alt}y"))
        nx = params.get(
            f"normalized_x{suffix}", params.get(f"{alt}normalized_x")
        )
        ny = params.get(
            f"normalized_y{suffix}", params.get(f"{alt}normalized_y")
        )
        point = resolve_point(geometry, x=x, y=y,
                              normalized_x=nx, normalized_y=ny)
        validate_point(geometry, point[0], point[1])
        return point

    def _dry_run_result(
        self,
        request: ActionRequest,
        safe_params: dict,
        plan: dict,
        started: float,
    ) -> ActionResult:
        record = self.evidence.record(
            request.action_id,
            "dry_run",
            {**dict(safe_params), "plan": sanitize(plan)},
        )
        return ActionResult(
            action_id=request.action_id,
            action_type=request.action_type,
            timestamp=now_ts(),
            requested_params=dict(safe_params),
            execution_result={
                "ok": True,
                "dry_run": True,
                "intended": sanitize(plan),
                "detail": f"would execute {request.action_type.value}",
            },
            duration_ms=self._elapsed_ms(started),
            verification=VerificationState.UNVERIFIED,
            evidence_ref=record.evidence_id,
            error_class=ErrorClass.NONE,
            error_message=None,
            risk=request.risk,
            dry_run=True,
        )

    def _dispatch(self, request: ActionRequest, plan: dict) -> Any:
        timing = self.config.timing
        backend = self.backend
        action = request.action_type
        params = request.params
        if action == ActionType.MOVE_MOUSE:
            x, y = plan["point"]
            return backend.move(x, y, timing.mouse_move_duration_ms)
        if action == ActionType.CLICK:
            x, y = plan["point"]
            return backend.click(
                x, y, str(params.get("button", "left")), 1
            )
        if action == ActionType.DOUBLE_CLICK:
            x, y = plan["point"]
            return backend.click(
                x, y, str(params.get("button", "left")), 2
            )
        if action == ActionType.RIGHT_CLICK:
            x, y = plan["point"]
            return backend.click(x, y, "right", 1)
        if action == ActionType.MOUSE_DOWN:
            x, y = plan["point"]
            return backend.mouse_down(x, y, str(params.get("button", "left")))
        if action == ActionType.MOUSE_UP:
            x, y = plan["point"]
            return backend.mouse_up(x, y, str(params.get("button", "left")))
        if action == ActionType.DRAG:
            fx, fy = plan["from"]
            tx, ty = plan["to"]
            return backend.drag(fx, fy, tx, ty, timing.drag_duration_ms)
        if action == ActionType.SCROLL:
            x, y = plan["point"]
            return backend.scroll(
                x, y, int(params.get("dx", 0)), int(params.get("dy", 0))
            )
        if action == ActionType.TYPE_TEXT:
            secret = params.get("secret")
            if isinstance(secret, SecretValue):
                return backend.type(
                    secret.reveal(), timing.typing_interval_ms, secret=True
                )
            return backend.type(
                str(params.get("text", "")), timing.typing_interval_ms
            )
        if action == ActionType.PRESS_KEY:
            return backend.key(str(params["key"]))
        if action == ActionType.HOTKEY:
            keys = [str(k) for k in params["keys"]]
            return backend.hotkey(keys)
        if action == ActionType.WAIT:
            time.sleep(min(plan["seconds"], request.timeout_seconds))
            return _OkResult({"waited_seconds": plan["seconds"]})
        if action == ActionType.SCREENSHOT:
            shot = backend.screenshot(
                region=plan.get("region"),
                active_window_only=bool(
                    params.get("active_window_only", False)
                ),
            )
            return _OkResult(
                {
                    "artifact_id": shot.artifact_id,
                    "width": shot.width,
                    "height": shot.height,
                    "scale_factor": shot.scale_factor,
                }
            )
        if action == ActionType.FOCUS_WINDOW:
            return backend.focus_window(
                str(params.get("app_name", params.get("target", ""))),
                str(params.get("window_title", "")),
            )
        if action == ActionType.ACTIVATE_APPLICATION:
            app_name = str(params.get("app_name", params.get("target", "")))
            if not app_name:
                raise CoordinateError("activate_application needs app_name")
            return backend.focus_window(app_name, "")
        if action == ActionType.GET_ACTIVE_WINDOW:
            active = backend.active_window()
            return _OkResult(
                {"app": active.app_name, "window": active.window_title}
            )
        raise CoordinateError(f"unsupported action {action.value}")

    def _blocked_result(
        self,
        request: ActionRequest,
        safe_params: dict,
        started: float,
        exc: Exception,
        secrets: list[str],
    ) -> ActionResult:
        self.session.status = SessionStatus.BLOCKED
        return _error_result(
            request,
            dict(safe_params),
            ErrorClass.PERMISSION_REQUIRED,
            sanitize_message(str(exc), secrets),
            VerificationState.BLOCKED_EXTERNAL,
            self._elapsed_ms(started),
        )

    def _backend_error(
        self,
        request: ActionRequest,
        safe_params: dict,
        started: float,
        exc: Exception,
        secrets: list[str],
    ) -> ActionResult:
        message = sanitize_message(f"{type(exc).__name__}: {exc}", secrets)
        logger.warning("computer backend error: %s", message)
        return _error_result(
            request,
            dict(safe_params),
            ErrorClass.BACKEND_FAILURE,
            message,
            VerificationState.FAILED,
            self._elapsed_ms(started),
        )

    def _map_backend_failure(
        self,
        request: ActionRequest,
        safe_params: dict,
        started: float,
        exec_result: Any,
        secrets: list[str],
    ) -> ActionResult:
        klass = str(getattr(exec_result, "error_class", "BACKEND_FAILURE"))
        message = sanitize_message(
            str(getattr(exec_result, "error_message", "backend failed")),
            secrets,
        )
        if klass == "PERMISSION_REQUIRED":
            self.session.status = SessionStatus.BLOCKED
            return _error_result(
                request,
                dict(safe_params),
                ErrorClass.PERMISSION_REQUIRED,
                message,
                VerificationState.BLOCKED_EXTERNAL,
                self._elapsed_ms(started),
            )
        if klass in ("TIMEOUT",):
            error_class = ErrorClass.TIMEOUT
        else:
            error_class = ErrorClass.BACKEND_FAILURE
        return _error_result(
            request,
            dict(safe_params),
            error_class,
            message,
            VerificationState.FAILED,
            self._elapsed_ms(started),
        )


@dataclass
class _OkResult:
    data: dict = field(default_factory=dict)
    ok: bool = True
    error_class: str = "NONE"
    error_message: str = ""
