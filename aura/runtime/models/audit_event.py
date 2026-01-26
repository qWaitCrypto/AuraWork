from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class AuditEventType(StrEnum):
    PLAN_GENERATED = "plan_generated"
    NODE_STARTED = "node_started"
    TOOL_CALLED = "tool_called"
    CHANGESET_READY = "changeset_ready"
    VERIFY_PASSED = "verify_passed"
    VERIFY_FAILED = "verify_failed"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    APPLIED_TO_MAINLINE = "applied_to_mainline"
    REVERTED = "reverted"
    RUN_CLOSED = "run_closed"


class AuditEvent(BaseModel):
    """
    AuditEvent data model per design doc §11.4.

    Stored as JSONL (one JSON object per line) by AuditStore.
    """

    event_id: UUID
    event_type: AuditEventType
    timestamp: datetime

    run_id: str | None = None
    task_id: str | None = None
    node_id: str | None = None

    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("run_id", "task_id", "node_id")
    @classmethod
    def _strip_empty_optional_ids(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        return s or None

