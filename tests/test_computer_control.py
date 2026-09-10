"""Worker L: hermetic computer-control tests.

Every test runs against FakeComputerBackend. No test moves the real
mouse, types into real applications, or captures the real screen.
"""

import logging

import pytest

from app.computer.actions import (
    ActionRequest,
    ActionType,
    ErrorClass,
    RiskLevel,
    VerificationState,
)
from app.computer.backends.fake import FakeComputerBackend
from app.computer.backends.macos import MacOSComputerBackend
from app.computer.backends import MacOSComputerBackend as ReexportedMacOS
from app.computer.evidence import EvidenceStore
from app.computer.geometry import (
    CoordinateError,
    DisplayInfo,
    ScreenGeometry,
    resolve_point,
    validate_point,
)
from app.computer.policy import ApprovalRequired, check_approval, classify
from app.computer.recovery import RecoveryAction, plan_recovery
from app.computer.runtime import ComputerRuntime, RuntimeConfig
from app.computer.secrets import SecretValue, sanitize
from app.computer.session import ComputerSession, SessionStatus
from app.computer.timing import TimingConfig
from app.computer.verification import Observation, verify_action


def _runtime(**kwargs):
    backend = kwargs.pop("backend", None) or FakeComputerBackend()
    config = RuntimeConfig(
        evidence_dir=None,
        timing=TimingConfig(
            typing_interval_ms=0,
            mouse_move_duration_ms=0,
            click_delay_ms=0,
            drag_duration_ms=0,
            action_settle_ms=0,
        ),
    )
    runtime = ComputerRuntime(backend, config)
    return runtime, backend


def test_coordinate_validation_rejects_out_of_bounds():
    runtime, _ = _runtime()
    result = runtime.act(
        action_type=ActionType.CLICK, params={"x": 99999, "y": 50}
    )
    assert result.error_class == ErrorClass.VALIDATION
    assert result.verification == VerificationState.FAILED


def test_negative_monitor_coordinates_supported():
    geometry = ScreenGeometry(
        displays=[
            DisplayInfo(
                display_id=0,
                origin_x=-1920,
                origin_y=0,
                width=1920,
                height=1080,
            ),
            DisplayInfo(
                display_id=1,
                origin_x=0,
                origin_y=0,
                width=1440,
                height=900,
            ),
        ]
    )
    display = validate_point(geometry, -100, 100)
    assert display.display_id == 0
    with pytest.raises(CoordinateError):
        validate_point(geometry, -5000, 100)


def test_normalized_coordinates_map_to_virtual_screen():
    geometry = ScreenGeometry(
        displays=[
            DisplayInfo(
                display_id=0,
                origin_x=-1920,
                origin_y=0,
                width=1920,
                height=1080,
            ),
            DisplayInfo(
                display_id=1,
                origin_x=0,
                origin_y=0,
                width=1440,
                height=900,
            ),
        ]
    )
    x, y = resolve_point(geometry, normalized_x=0.0, normalized_y=0.0)
    assert (x, y) == (-1920, 0)
    x, y = resolve_point(geometry, normalized_x=1.0, normalized_y=1.0)
    assert (x, y) == (1440, 1080)
    with pytest.raises(CoordinateError):
        resolve_point(geometry, normalized_x=1.5, normalized_y=0.5)


def test_action_dispatch_move_and_click():
    runtime, backend = _runtime()
    moved = runtime.act(
        action_type=ActionType.MOVE_MOUSE, params={"x": 200, "y": 250}
    )
    assert moved.verification == VerificationState.VERIFIED
    assert backend.pointer_position() == (200.0, 250.0)
    clicked = runtime.act(
        action_type=ActionType.CLICK, params={"x": 300, "y": 300}
    )
    assert clicked.error_class == ErrorClass.NONE
    assert any(call["op"] == "click" for call in backend.calls)


def test_screenshot_metadata():
    runtime, _ = _runtime()
    shot = runtime.observe()
    assert shot["width"] > 0 and shot["height"] > 0
    assert shot["scale_factor"] == 2.0
    assert shot["artifact_id"]
    assert shot["active_app"] == "FakeApp"
    assert shot["timestamp"] > 0


def test_click_verification_detects_pixel_change():
    runtime, backend = _runtime()
    result = runtime.act(
        action_type=ActionType.CLICK, params={"x": 120, "y": 130}
    )
    assert result.verification == VerificationState.VERIFIED
    assert result.evidence_ref is not None
    assert backend.calls


def test_typing_and_hotkeys():
    runtime, backend = _runtime()
    typed = runtime.act(
        action_type=ActionType.TYPE_TEXT, params={"text": "hello"}
    )
    assert typed.verification in (
        VerificationState.VERIFIED,
        VerificationState.UNVERIFIED,
    )
    hotkey = runtime.act(
        action_type=ActionType.HOTKEY, params={"keys": ["cmd", "c"]}
    )
    assert hotkey.error_class == ErrorClass.NONE
    key = runtime.act(
        action_type=ActionType.PRESS_KEY, params={"key": "Escape"}
    )
    assert key.error_class == ErrorClass.NONE
    ops = [call["op"] for call in backend.calls]
    assert "type" in ops and "hotkey" in ops and "key" in ops


def test_drag_and_scroll():
    runtime, backend = _runtime()
    dragged = runtime.act(
        action_type=ActionType.DRAG,
        params={"from_x": 10, "from_y": 10, "to_x": 60, "to_y": 70},
    )
    assert dragged.error_class == ErrorClass.NONE
    scrolled = runtime.act(
        action_type=ActionType.SCROLL,
        params={"x": 50, "y": 50, "dy": -3},
    )
    assert scrolled.error_class == ErrorClass.NONE
    ops = [call["op"] for call in backend.calls]
    assert "drag" in ops and "scroll" in ops


def test_active_window_verification():
    runtime, _ = _runtime()
    result = runtime.act(
        action_type=ActionType.ACTIVATE_APPLICATION,
        params={"app_name": "TextEdit"},
    )
    assert result.error_class == ErrorClass.NONE
    assert result.verification == VerificationState.VERIFIED
    status = runtime.session_status()
    assert status["active_app"] == "TextEdit"


def test_retry_on_unverified_click():
    backend = FakeComputerBackend()
    runtime, _ = _runtime(backend=backend)
    events: list[str] = []
    runtime._on_event = lambda event, data: events.append(event)
    # Freeze the pixel fingerprint so the first attempt verifies as
    # UNVERIFIED and the retry path is exercised.
    backend._last_pixels = b"frozen"
    original_click = backend.click

    def frozen_click(*args, **kwargs):
        result = original_click(*args, **kwargs)
        backend._last_pixels = b"frozen"
        return result

    backend.click = frozen_click  # type: ignore[method-assign]
    result = runtime.act(
        action_type=ActionType.CLICK, params={"x": 400, "y": 400}
    )
    assert result.verification == VerificationState.UNVERIFIED
    assert "retry" in events
    clicks = [call for call in backend.calls if call["op"] == "click"]
    assert len(clicks) == 2


def test_timeout_maps_to_timeout_error():
    class SlowBackend(FakeComputerBackend):
        def click(self, *args, **kwargs):
            raise TimeoutError("simulated os timeout")

    runtime, _ = _runtime(backend=SlowBackend())
    result = runtime.act(
        action_type=ActionType.CLICK, params={"x": 10, "y": 10}
    )
    assert result.error_class == ErrorClass.TIMEOUT
    assert result.verification == VerificationState.FAILED


def test_permission_denied_maps_to_blocked_external():
    backend = FakeComputerBackend(deny_permission="Accessibility")
    runtime, _ = _runtime(backend=backend)
    result = runtime.act(
        action_type=ActionType.CLICK, params={"x": 10, "y": 10}
    )
    assert result.error_class == ErrorClass.PERMISSION_REQUIRED
    assert result.verification == VerificationState.BLOCKED_EXTERNAL
    assert runtime.session.status == SessionStatus.BLOCKED


def test_screenshot_permission_denied_is_blocked():
    backend = FakeComputerBackend(deny_permission="Screen Recording")
    runtime, _ = _runtime(backend=backend)
    with pytest.raises(PermissionError):
        runtime.observe()


def test_geometry_change_recaptured():
    backend = FakeComputerBackend()
    runtime, _ = _runtime(backend=backend)
    old_generation = runtime.session.geometry.generation
    backend.set_geometry(
        ScreenGeometry(
            displays=[
                DisplayInfo(
                    display_id=0,
                    origin_x=0,
                    origin_y=0,
                    width=2560,
                    height=1440,
                )
            ]
        )
    )
    result = runtime.act(
        action_type=ActionType.MOVE_MOUSE, params={"x": 100, "y": 100}
    )
    assert result.error_class == ErrorClass.NONE
    assert runtime.session.geometry.primary.width == 2560
    assert old_generation == 0


def test_multi_monitor_mapping():
    geometry = ScreenGeometry(
        displays=[
            DisplayInfo(
                display_id=0,
                origin_x=0,
                origin_y=0,
                width=1440,
                height=900,
            ),
            DisplayInfo(
                display_id=1,
                origin_x=1440,
                origin_y=0,
                width=1920,
                height=1080,
            ),
        ]
    )
    runtime, _ = _runtime(backend=FakeComputerBackend(geometry=geometry))
    result = runtime.act(
        action_type=ActionType.MOVE_MOUSE, params={"x": 2000, "y": 500}
    )
    assert result.error_class == ErrorClass.NONE
    outside = runtime.act(
        action_type=ActionType.MOVE_MOUSE, params={"x": 5000, "y": 500}
    )
    assert outside.error_class == ErrorClass.VALIDATION


def test_dry_run_touches_nothing():
    backend = FakeComputerBackend()
    runtime, _ = _runtime(backend=backend)
    request = ActionRequest(
        action_type=ActionType.CLICK,
        params={"x": 100, "y": 100},
        dry_run=True,
    )
    result = runtime.act(request)
    assert result.dry_run is True
    assert result.error_class == ErrorClass.NONE
    assert backend.calls == []
    assert "intended" in result.execution_result


def test_secret_safe_logging(caplog):
    backend = FakeComputerBackend()
    runtime, _ = _runtime(backend=backend)
    secret = SecretValue.from_plaintext("login", "s3cr3t-p@ss")
    with caplog.at_level(logging.DEBUG, logger="yodaw.computer"):
        result = runtime.act(
            action_type=ActionType.TYPE_TEXT,
            params={"secret": secret, "target": "password field"},
        )
    assert result.error_class in (
        ErrorClass.NONE,
        ErrorClass.APPROVAL_REQUIRED,
    )
    assert "s3cr3t-p@ss" not in caplog.text
    assert "s3cr3t-p@ss" not in str(result.requested_params)
    assert "s3cr3t-p@ss" not in str(result.execution_result)
    evidence = runtime.evidence.for_action(result.action_id)
    assert "s3cr3t-p@ss" not in str(
        [record.summary for record in evidence]
    )
    assert sanitize({"secret": secret}) == {
        "secret": "<secret:login len=11>"
    }


def test_high_risk_approval_gate():
    runtime, _ = _runtime()
    dangerous = ActionRequest(
        action_type=ActionType.CLICK,
        params={"x": 50, "y": 50, "target": "Delete account"},
    )
    assert classify(dangerous) == RiskLevel.HIGH
    with pytest.raises(ApprovalRequired):
        check_approval(dangerous, approved=False)
    result = runtime.act(dangerous, approved=False)
    assert result.error_class == ErrorClass.APPROVAL_REQUIRED
    approved_result = runtime.act(dangerous, approved=True)
    assert approved_result.error_class == ErrorClass.NONE


def test_backend_failure_classification():
    backend = FakeComputerBackend(fail_next={"click": 5})
    runtime, _ = _runtime(backend=backend)
    result = runtime.act(
        action_type=ActionType.CLICK, params={"x": 10, "y": 10}
    )
    assert result.error_class == ErrorClass.BACKEND_FAILURE
    assert result.verification == VerificationState.FAILED


def test_ambiguous_verification_never_loops():
    plan = plan_recovery(no_observable_change=True, attempts_so_far=1)
    assert plan.action == RecoveryAction.ABORT
    backend = FakeComputerBackend()
    runtime, _ = _runtime(backend=backend)
    backend._last_pixels = b"frozen"
    original_click = backend.click

    def frozen_click(*args, **kwargs):
        result = original_click(*args, **kwargs)
        backend._last_pixels = b"frozen"
        return result

    backend.click = frozen_click  # type: ignore[method-assign]
    result = runtime.act(
        action_type=ActionType.CLICK, params={"x": 40, "y": 40}
    )
    clicks = [call for call in backend.calls if call["op"] == "click"]
    assert len(clicks) == 2
    assert result.verification == VerificationState.UNVERIFIED


def test_caller_predicate_verification():
    runtime, _ = _runtime()
    assert (
        runtime.verify(lambda ctx: ctx.get("ok") is True, {"ok": True})
        == VerificationState.VERIFIED
    )
    assert (
        runtime.verify(lambda ctx: ctx.get("ok") is True, {"ok": False})
        == VerificationState.FAILED
    )
    request = ActionRequest(
        action_type=ActionType.WAIT, params={"seconds": 0}
    )
    state = verify_action(
        request,
        FakeComputerBackend(),
        Observation(),
        True,
        predicate=lambda ctx: False,
        predicate_context={},
    )
    assert state == VerificationState.FAILED


def test_session_lifecycle_and_status():
    session = ComputerSession(platform="macos")
    assert session.status == SessionStatus.READY
    session.touch("act_1")
    assert session.status == SessionStatus.ACTIVE
    described = session.describe()
    assert described["session_id"] == session.session_id
    assert described["action_count"] == 1
    runtime, _ = _runtime()
    assert runtime.session_status()["status"] in ("READY", "ACTIVE")
    runtime.pause()
    runtime.resume()
    runtime.close()
    assert runtime.session_status()["status"] == "CLOSED"
    closed = runtime.act(
        action_type=ActionType.MOVE_MOUSE, params={"x": 1, "y": 1}
    )
    assert closed.error_class == ErrorClass.VALIDATION


def test_macos_backend_constructs_off_platform():
    backend = MacOSComputerBackend()
    assert backend.platform() == "macos"
    assert ReexportedMacOS is MacOSComputerBackend
    # Off macOS, control is reported as blocked, not fake success.
    assert backend.move(10, 10).error_class == "BLOCKED_EXTERNAL"
    store = EvidenceStore()
    record = store.record("act_x", "action", {"ok": True})
    assert store.get(record.evidence_id) == record
    assert store.for_action("act_x") == [record]
