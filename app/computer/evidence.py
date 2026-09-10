"""Lightweight evidence ledger: every action leaves a trace."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class EvidenceRecord:
    evidence_id: str
    action_id: str
    kind: str
    summary: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)


class EvidenceStore:
    """In-memory evidence index with optional artifact directory."""

    def __init__(self, artifact_dir: str | None = None) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        self.artifact_dir = artifact_dir

    def record(
        self,
        action_id: str,
        kind: str,
        summary: dict | None = None,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            evidence_id=f"ev_{uuid.uuid4().hex[:12]}",
            action_id=action_id,
            kind=kind,
            summary=dict(summary or {}),
        )
        self._records[record.evidence_id] = record
        return record

    def save_screenshot_artifact(
        self, png_bytes: bytes, evidence_id: str
    ) -> str | None:
        if not self.artifact_dir or not png_bytes:
            return None
        try:
            os.makedirs(self.artifact_dir, exist_ok=True)
            path = os.path.join(self.artifact_dir, f"{evidence_id}.png")
            with open(path, "wb") as handle:
                handle.write(png_bytes)
            return path
        except OSError:
            return None

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._records.get(evidence_id)

    def for_action(self, action_id: str) -> list[EvidenceRecord]:
        return [
            record
            for record in self._records.values()
            if record.action_id == action_id
        ]
