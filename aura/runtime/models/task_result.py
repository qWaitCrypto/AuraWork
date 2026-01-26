from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskStatus(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    NEEDS_HUMAN = "needs_human"


class OperationType(StrEnum):
    CREATE = "create"
    OVERWRITE = "overwrite"
    MOVE = "move"
    RENAME = "rename"
    DELETE = "delete"


class OperationPlanItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    op: OperationType
    target: str
    from_: str | None = Field(default=None, alias="from")
    reason: str | None = None

    @field_validator("target")
    @classmethod
    def _non_empty_target(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("target must be a non-empty string.")
        return v


class OperationBreakdown(BaseModel):
    create: int | None = Field(default=None, ge=0)
    overwrite: int | None = Field(default=None, ge=0)
    move: int | None = Field(default=None, ge=0)
    rename: int | None = Field(default=None, ge=0)
    delete: int | None = Field(default=None, ge=0)


class OperationPlan(BaseModel):
    summary: str | None = None
    total_ops: int | None = Field(default=None, ge=0)
    breakdown: OperationBreakdown | None = None
    items: list[OperationPlanItem] | None = None

    @model_validator(mode="after")
    def _validate_totals(self) -> "OperationPlan":
        if self.total_ops is None:
            return self
        if self.items is not None and len(self.items) > self.total_ops:
            raise ValueError("total_ops must be >= len(items) when both are provided.")
        return self


class ArtifactType(StrEnum):
    DOC = "doc"
    SHEET = "sheet"
    INDEX = "index"
    REPORT = "report"
    OPERATION_PLAN = "operation_plan"
    EVIDENCE = "evidence"
    DIFF = "diff"
    AUDIT = "audit"
    OTHER = "other"


class TrustLevel(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class ArtifactProvenance(BaseModel):
    source_url: str | None = None
    timestamp: datetime | None = None
    hash: str | None = None


class ArtifactRecord(BaseModel):
    artifact_id: str
    type: ArtifactType
    trust_level: TrustLevel | None = None
    instruction_suspected: bool | None = None
    path: str
    producer: str | None = None
    summary: str | None = None
    provenance: ArtifactProvenance | None = None

    @field_validator("artifact_id", "path")
    @classmethod
    def _non_empty_str(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Must be a non-empty string.")
        return v


class ProposalType(StrEnum):
    ADD_NODE = "add_node"
    MODIFY_NODE = "modify_node"
    ADD_VALIDATION = "add_validation"
    CLARIFY = "clarify"
    ABORT = "abort"


class Proposal(BaseModel):
    type: ProposalType | None = None
    reason: str | None = None
    spec: dict[str, Any] | None = None


class TaskMetrics(BaseModel):
    duration_ms: int | None = Field(default=None, ge=0)
    files_processed: int | None = Field(default=None, ge=0)
    errors_count: int | None = Field(default=None, ge=0)


class TaskResult(BaseModel):
    """TaskResult data model per design doc §11.3."""

    status: TaskStatus
    operation_plan: OperationPlan | None = None
    artifacts: list[ArtifactRecord] | None = None
    logs: list[str] | None = None
    proposals: list[Proposal] | None = None
    questions: list[str] | None = None
    risk_flags: list[str] | None = None
    metrics: TaskMetrics | None = None
