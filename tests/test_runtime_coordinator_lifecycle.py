"""A stopped coordinator must never be handed out again.

Reproduces the full-suite stall that made four mission-runtime tests fail
only when the suite ran as a whole:

1. any ``with TestClient(app)`` block exits the app lifespan, which calls
   ``coordinator.stop()``;
2. ``stop()`` sets ``_shutting_down`` and ``_stop`` permanently, so the
   cached coordinator can never claim another mission;
3. the process-wide accessor kept returning that dead instance, so every
   mission created afterwards was accepted and then sat in ``QUEUED``
   forever with no error.

The minimal reproducer was just two tests in sequence
(``test_background_tasks.py::test_task_api_contract`` followed by
``test_tenancy.py::test_tenant_mission_carries_identity_priority_and_quota``).
"""

from fastapi.testclient import TestClient

import app.main as main_module


def test_accessor_rebuilds_after_app_shutdown():
    with TestClient(main_module.app):
        first = main_module.get_coordinator()
        assert first is not None
        assert not first.is_stopped()

    # Leaving the lifespan stops the coordinator for good.
    assert first.is_stopped()

    # The next caller must get a live coordinator instead of the corpse.
    second = main_module.get_coordinator()
    assert second is not None
    assert second is not first
    assert not second.is_stopped()


def test_stopped_coordinator_reports_itself_stopped():
    coordinator = main_module.get_coordinator()
    assert coordinator is not None
    assert coordinator.is_stopped() is False
    coordinator.stop(drain=False, timeout=5)
    assert coordinator.is_stopped() is True
    # And the accessor heals the singleton again.
    replacement = main_module.get_coordinator()
    assert replacement is not None
    assert not replacement.is_stopped()
