"""Corruption recovery for loop history.

If the SQLite file backing proposals/activations is corrupted, this
module quarantines the damaged file (renamed with a ``.corrupt`` plus
timestamp suffix, never deleted) and rebuilds empty stores so the
loop can continue from a known-good state. The recovery itself is
audited where possible; when the audit tables live in the same
corrupt file, the recovery report records that the trail was reset.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.improvement.audit import LoopAudit


@dataclass
class RecoveryReport:
    path: str
    quarantined_to: str | None
    proposals_rebuilt: bool
    activations_rebuilt: bool
    audit_noted: bool
    reason: str


def recover_history(path: Path | str) -> RecoveryReport:
    """Quarantine a corrupt history file and rebuild empty stores."""
    db_path = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantined: str | None = None
    reason = "unreadable history file"

    if db_path.exists():
        target = db_path.with_name(f"{db_path.name}.corrupt.{stamp}")
        shutil.move(str(db_path), str(target))
        quarantined = str(target)
        reason = f"corrupt history quarantined to {target.name}"

    from app.improvement.store import ProposalStore
    from app.improvement.versioning import ActivationStore

    proposals = ProposalStore(path=db_path)
    activations = ActivationStore(path=db_path, proposals=proposals)

    audit_noted = False
    try:
        LoopAudit(path=db_path).record(
            "loop.history_recovered",
            {"quarantined_to": quarantined, "reason": reason},
        )
        audit_noted = True
    except Exception:
        audit_noted = False

    _ = (proposals.count(), len(activations.history()))

    return RecoveryReport(
        path=str(db_path),
        quarantined_to=quarantined,
        proposals_rebuilt=True,
        activations_rebuilt=True,
        audit_noted=audit_noted,
        reason=reason,
    )
