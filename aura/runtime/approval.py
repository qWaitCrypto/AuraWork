from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"


class ApprovalDecision(str, Enum):
    APPROVE = "approve"
    DENY = "deny"
    EDIT = "edit"
    DRY_RUN = "dry_run"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    session_id: str
    request_id: str
    created_at: int

    status: ApprovalStatus = ApprovalStatus.PENDING
    turn_id: str | None = None

    action_summary: str = ""
    risk_level: str | None = None
    options: list[str] = field(default_factory=list)

    reason: str | None = None
    diff_ref: dict[str, Any] | None = None

    resume_kind: str | None = None
    resume_payload: dict[str, Any] = field(default_factory=dict)

    decision: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "status": self.status.value,
            "action_summary": self.action_summary,
            "options": list(self.options),
            "resume_payload": dict(self.resume_payload),
        }
        if self.turn_id is not None:
            out["turn_id"] = self.turn_id
        if self.risk_level is not None:
            out["risk_level"] = self.risk_level
        if self.reason is not None:
            out["reason"] = self.reason
        if self.diff_ref is not None:
            out["diff_ref"] = self.diff_ref
        if self.resume_kind is not None:
            out["resume_kind"] = self.resume_kind
        if self.decision is not None:
            out["decision"] = self.decision
        return out

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ApprovalRecord":
        status_raw = str(raw.get("status") or ApprovalStatus.PENDING.value)
        try:
            status = ApprovalStatus(status_raw)
        except ValueError:
            status = ApprovalStatus.PENDING
        return ApprovalRecord(
            approval_id=str(raw["approval_id"]),
            session_id=str(raw["session_id"]),
            request_id=str(raw["request_id"]),
            created_at=int(raw["created_at"]),
            status=status,
            turn_id=str(raw["turn_id"]) if raw.get("turn_id") is not None else None,
            action_summary=str(raw.get("action_summary") or ""),
            risk_level=str(raw["risk_level"]) if raw.get("risk_level") is not None else None,
            options=list(raw.get("options") or []),
            reason=str(raw["reason"]) if raw.get("reason") is not None else None,
            diff_ref=dict(raw["diff_ref"]) if raw.get("diff_ref") is not None else None,
            resume_kind=str(raw["resume_kind"]) if raw.get("resume_kind") is not None else None,
            resume_payload=dict(raw.get("resume_payload") or {}),
            decision=dict(raw["decision"]) if raw.get("decision") is not None else None,
        )

