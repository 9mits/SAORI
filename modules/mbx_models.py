from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CaseNote:
    author_id: int
    note: str
    created_at: str

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CaseNote":
        return cls(
            author_id=int(payload.get("author_id", 0) or 0),
            note=str(payload.get("note", "") or ""),
            created_at=str(payload.get("created_at", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "author_id": self.author_id,
            "note": self.note,
            "created_at": self.created_at,
        }


@dataclass
class CaseMetadata:
    status: str = "open"
    resolution_state: str = "pending"
    tags: List[str] = field(default_factory=list)
    evidence_links: List[str] = field(default_factory=list)
    linked_cases: List[int] = field(default_factory=list)
    assigned_moderator: Optional[int] = None
    internal_notes: List[CaseNote] = field(default_factory=list)
    action_id: Optional[str] = None

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "CaseMetadata":
        notes = []
        for note in record.get("internal_notes", []):
            if isinstance(note, dict):
                notes.append(CaseNote.from_dict(note))

        return cls(
            status=str(record.get("status", "open") or "open"),
            resolution_state=str(record.get("resolution_state", "pending") or "pending"),
            tags=[str(tag) for tag in record.get("tags", []) if str(tag).strip()],
            evidence_links=[str(url) for url in record.get("evidence_links", []) if str(url).strip()],
            linked_cases=[
                int(case_id)
                for case_id in record.get("linked_cases", [])
                if isinstance(case_id, int) or str(case_id).isdigit()
            ],
            assigned_moderator=(
                int(record["assigned_moderator"])
                if record.get("assigned_moderator") is not None and str(record.get("assigned_moderator")).isdigit()
                else None
            ),
            internal_notes=notes,
            action_id=(str(record.get("action_id")).strip() if record.get("action_id") else None),
        )

    def apply_to_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["status"] = self.status
        record["resolution_state"] = self.resolution_state
        record["tags"] = list(self.tags)
        record["evidence_links"] = list(self.evidence_links)
        record["linked_cases"] = list(self.linked_cases)
        record["assigned_moderator"] = self.assigned_moderator
        record["internal_notes"] = [note.to_dict() for note in self.internal_notes]
        if self.action_id:
            record["action_id"] = self.action_id
        return record


@dataclass
class EscalationStep:
    minimum_points: float
    mode: str
    multiplier: int = 1
    force_ban: bool = False
    label: str = ""

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "EscalationStep":
        return cls(
            minimum_points=float(payload.get("minimum_points", 0)),
            mode=str(payload.get("mode", "base") or "base"),
            multiplier=max(1, int(payload.get("multiplier", 1) or 1)),
            force_ban=bool(payload.get("force_ban", False)),
            label=str(payload.get("label", "") or ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "minimum_points": self.minimum_points,
            "mode": self.mode,
            "multiplier": self.multiplier,
            "force_ban": self.force_ban,
            "label": self.label,
        }


@dataclass
class ValidationFinding:
    level: str
    section: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "level": self.level,
            "section": self.section,
            "message": self.message,
        }
