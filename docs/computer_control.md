# Computer Control Runtime (Worker L)

Production-grade desktop control for YODAW through mouse, keyboard,
screenshots, and **verified** UI actions.

Pipeline: `OBSERVE → UNDERSTAND → PLAN → ACT → VERIFY → RECOVER`.
Blind coordinate macros are not the control mechanism: every action
is validated, executed against a backend, then verified against
fresh observation before it is reported as successful.

## Layout

- `app/computer/actions.py` — action model (`ActionType`,
  `ActionRequest`, `ActionResult`, risk levels, verification and
  error classifications).
- `app/computer/backend.py` — `ComputerBackend` interface.
- `app/computer/backends/macos.py` — macOS backend (Quartz when
  available, `osascript`/`screencapture` otherwise).
- `app/computer/backends/fake.py` — hermetic fake for tests.
- `app/computer/geometry.py` — Retina-aware coordinates,
  multi-monitor bounds, normalized coordinates.
- `app/computer/timing.py` — human-like, reliability-first timing.
- `app/computer/policy.py` — risk classification + approval gate.
- `app/computer/secrets.py` — `SecretValue` redaction.
- `app/computer/session.py` — session lifecycle.
- `app/computer/recovery.py` — bounded recovery plans.
- `app/computer/verification.py` — verification strategies.
- `app/computer/runtime.py` — `ComputerRuntime` orchestrator.
- `app/computer/smoke.py` — opt-in manual smoke probe.

## macOS permissions

No root required. Grant per-app in
System Settings → Privacy & Security:

| Capability | Permission | Missing → |
|---|---|---|
| Mouse, keyboard, window focus | Accessibility | `BLOCKED_EXTERNAL` / `PERMISSION_REQUIRED` |
| Screenshots (`screencapture`) | Screen Recording | `BLOCKED_EXTERNAL` / `PERMISSION_REQUIRED` |

Permission absence is reported as `BLOCKED_EXTERNAL`, never as
`TASK_FAIL`, and the runtime never bypasses OS security prompts.

## Integration surface (for future planners)

```python
from app.computer.backends.fake import FakeComputerBackend
from app.computer.runtime import ComputerRuntime
from app.computer.actions import ActionType

computer = ComputerRuntime(FakeComputerBackend())
shot = computer.observe()                       # OBSERVE
result = computer.act(action_type=ActionType.CLICK,
                      params={"x": 100, "y": 200})  # PLAN→ACT→VERIFY→RECOVER
state = computer.verify(lambda ctx: True, {})   # VERIFY
status = computer.session_status()              # session snapshot
```

`computer.act()` accepts either an `ActionRequest` or
`(action_type, params)` kwargs, plus `approved=True` for HIGH-risk
actions and an optional `verify_predicate` / `verify_context`.

## Verification

OS success is never trusted alone. Strategies:

- `screenshot-before / screenshot-after` (pixel fingerprint change),
- active-window changed,
- expected app became active,
- caller-provided predicate (`verify_predicate`).

Outcomes per action: `VERIFIED`, `UNVERIFIED`, `FAILED`,
`BLOCKED_EXTERNAL`. One bounded retry is attempted for safe,
retryable actions that come back `UNVERIFIED`; ambiguous state
after retry aborts instead of looping.

## Safety

- Risk: LOW (move, scroll, screenshot, focus), MEDIUM (click,
  typing, hotkeys), HIGH (destructive targets, password/security
  fields, install/uninstall, delete, payment, system settings,
  permission changes — including keyword escalation in params).
- HIGH-risk actions need `approved=True` (or
  `params={"approved": True}`), else `APPROVAL_REQUIRED`.
- `SecretValue.from_plaintext(id, text)` types credentials without
  them ever reaching logs, evidence, or error messages.
- `dry_run=True` validates and reports intent without touching
  the desktop.
- Coordinates outside the virtual screen are rejected before
  execution (`VALIDATION`, no click happens).

## Manual smoke (opt-in only)

```bash
python3 -m app.computer.smoke --confirm
python3 -m app.computer.smoke --confirm --nudge-pointer
```

Takes a screenshot, reports the active window and pointer, and —
only with `--nudge-pointer` — moves the pointer 1px and back.
Never clicks or types. Never runs under pytest.
