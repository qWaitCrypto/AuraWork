from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    OPERATION_STARTED = "operation_started"
    OPERATION_PROGRESS = "operation_progress"
    OPERATION_COMPLETED = "operation_completed"
    OPERATION_FAILED = "operation_failed"
    OPERATION_CANCELLED = "operation_cancelled"

    MODEL_SELECTED = "model_selected"
    MODEL_RESOLUTION_FAILED = "model_resolution_failed"

    LLM_REQUEST_STARTED = "llm_request_started"
    LLM_THINKING_DELTA = "llm_thinking_delta"
    LLM_RESPONSE_DELTA = "llm_response_delta"
    LLM_RESPONSE_COMPLETED = "llm_response_completed"
    LLM_REQUEST_FAILED = "llm_request_failed"

    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"

    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_PROGRESS = "tool_call_progress"
    TOOL_CALL_END = "tool_call_end"

    SUBAGENT_APPROVER_STARTED = "subagent_approver_started"
    SUBAGENT_APPROVER_COMPLETED = "subagent_approver_completed"

    PLAN_UPDATE = "plan_update"

    RUN_PAUSED = "run_paused"


class OpKind(str, Enum):
    CHAT = "chat"
    COMPACT = "compact"
    APPROVAL_DECISION = "approval_decision"


@dataclass(frozen=True, slots=True)
class Op:
    kind: str
    payload: dict[str, Any]
    session_id: str
    request_id: str
    timestamp: int
    turn_id: str | None = None
    mode: str | None = None
    schema_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "payload": self.payload,
            "session_id": self.session_id,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
        }
        if self.turn_id is not None:
            out["turn_id"] = self.turn_id
        if self.mode is not None:
            out["mode"] = self.mode
        if self.schema_version is not None:
            out["schema_version"] = self.schema_version
        return out

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Op":
        return Op(
            kind=str(raw["kind"]),
            payload=dict(raw.get("payload") or {}),
            session_id=str(raw["session_id"]),
            request_id=str(raw["request_id"]),
            timestamp=int(raw["timestamp"]),
            turn_id=str(raw["turn_id"]) if raw.get("turn_id") is not None else None,
            mode=str(raw["mode"]) if raw.get("mode") is not None else None,
            schema_version=str(raw["schema_version"]) if raw.get("schema_version") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    payload: dict[str, Any]
    session_id: str
    event_id: str
    timestamp: int
    sequence: int | None = None
    request_id: str | None = None
    turn_id: str | None = None
    step_id: str | None = None
    schema_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": self.kind,
            "payload": self.payload,
            "session_id": self.session_id,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
        }
        if self.sequence is not None:
            out["sequence"] = self.sequence
        if self.request_id is not None:
            out["request_id"] = self.request_id
        if self.turn_id is not None:
            out["turn_id"] = self.turn_id
        if self.step_id is not None:
            out["step_id"] = self.step_id
        if self.schema_version is not None:
            out["schema_version"] = self.schema_version
        return out

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Event":
        return Event(
            kind=str(raw["kind"]),
            payload=dict(raw.get("payload") or {}),
            session_id=str(raw["session_id"]),
            event_id=str(raw["event_id"]),
            timestamp=int(raw["timestamp"]),
            sequence=int(raw["sequence"]) if raw.get("sequence") is not None else None,
            request_id=str(raw["request_id"]) if raw.get("request_id") is not None else None,
            turn_id=str(raw["turn_id"]) if raw.get("turn_id") is not None else None,
            step_id=str(raw["step_id"]) if raw.get("step_id") is not None else None,
            schema_version=str(raw["schema_version"]) if raw.get("schema_version") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    artifact_kind: str
    locator: str
    created_at: int
    sha256: str | None = None
    size_bytes: int | None = None
    mime: str | None = None
    summary: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "locator": self.locator,
            "created_at": self.created_at,
        }
        if self.sha256 is not None:
            out["sha256"] = self.sha256
        if self.size_bytes is not None:
            out["size_bytes"] = self.size_bytes
        if self.mime is not None:
            out["mime"] = self.mime
        if self.summary is not None:
            out["summary"] = self.summary
        if self.meta:
            out["meta"] = self.meta
        return out

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ArtifactRef":
        return ArtifactRef(
            artifact_id=str(raw["artifact_id"]),
            artifact_kind=str(raw["artifact_kind"]),
            locator=str(raw["locator"]),
            created_at=int(raw["created_at"]),
            sha256=str(raw["sha256"]) if raw.get("sha256") is not None else None,
            size_bytes=int(raw["size_bytes"]) if raw.get("size_bytes") is not None else None,
            mime=str(raw["mime"]) if raw.get("mime") is not None else None,
            summary=str(raw["summary"]) if raw.get("summary") is not None else None,
            meta=dict(raw.get("meta") or {}),
        )
