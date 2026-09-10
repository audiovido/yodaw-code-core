"""Audit trail integration for the self-improving loop.

Every loop state change is appended to the existing tamper-evident
tenant audit trail (:class:`app.tenants.audit.AuditStore`) — mining,
clustering, proposal creation/suppression, validation outcomes,
approval decisions, activations, rollbacks, and loop runs. The loop
never keeps a separate shadow log; the audit store is the evidence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.storage.db import DB_PATH
from app.tenants.audit import AuditStore

LOOP_ACTOR = "self-improving-loop"

ACTIONS = (
    "loop.mined",
    "loop.clustered",
    "loop.proposal_created",
    "loop.proposal_duplicate_suppressed",
    "loop.proposal_rejected_unsafe",
    "loop.validated",
    "loop.regression_checked",
    "loop.submitted",
    "loop.approved",
    "loop.rejected",
    "loop.activated",
    "loop.rolled_back",
    "loop.run",
    "loop.history_recovered",
)


class LoopAudit:
    """Thin wrapper recording loop events in the shared audit trail."""

    def __init__(
        self,
        path: Path | str = DB_PATH,
        store: AuditStore | None = None,
    ):
        self.store = store or AuditStore(path)

    def record(self, action: str, data: dict[str, Any] | None = None) -> dict:
        if action not in ACTIONS:
            raise ValueError(f"unknown loop audit action: {action}")
        return self.store.append(
            actor=LOOP_ACTOR, action=action, data=dict(data or {})
        )

    def events(self, action: str | None = None) -> list[dict]:
        if action is not None and action not in ACTIONS:
            raise ValueError(f"unknown loop audit action: {action}")
        return self.store.query(action=action, limit=1000)

    def verify(self) -> dict:
        return self.store.verify()
