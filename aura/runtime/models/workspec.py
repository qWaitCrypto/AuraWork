from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class WorkInputType(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    TEXT = "text"
    CONNECTOR_OBJECT = "connector_object"
    URL = "url"


class ExpectedOutputType(StrEnum):
    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    INDEX = "index"
    REPORT = "report"
    OTHER = "other"


class IntentItem(BaseModel):
    id: str
    text: str

    @field_validator("id", "text")
    @classmethod
    def _non_empty_str(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Must be a non-empty string.")
        return v


class WorkInput(BaseModel):
    type: WorkInputType | None = None
    path: str | None = None
    description: str | None = None


class ExpectedOutput(BaseModel):
    type: ExpectedOutputType
    format: str
    path: str | None = None

    @field_validator("format")
    @classmethod
    def _non_empty_format(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("format must be a non-empty string.")
        return v


class Constraints(BaseModel):
    style: str | None = None
    template: str | None = None
    deadline: datetime | None = None
    forbidden: list[str] | None = None


class ResourceScope(BaseModel):
    workspace_roots: list[str] | None = None
    file_type_allowlist: list[str] | None = None
    domain_allowlist: list[str] | None = None


class ApprovalPolicy(BaseModel):
    default_level: int | None = Field(default=None, ge=0, le=4)
    auto_approve_below: int | None = None
    require_approval_for: list[str] | None = None

    @model_validator(mode="after")
    def _validate_levels(self) -> "ApprovalPolicy":
        if self.auto_approve_below is None:
            return self
        if self.auto_approve_below < 0 or self.auto_approve_below > 4:
            raise ValueError("auto_approve_below must be between 0 and 4.")
        if self.default_level is not None and self.auto_approve_below > self.default_level:
            raise ValueError("auto_approve_below must be <= default_level when default_level is set.")
        return self


class WorkSpec(BaseModel):
    """WorkSpec data model per design doc §11.1."""

    goal: str
    intent_items: list[IntentItem] | None = None
    inputs: list[WorkInput] | None = None
    expected_outputs: list[ExpectedOutput]
    constraints: Constraints | None = None
    resource_scope: ResourceScope
    approval_policy: ApprovalPolicy | None = None

    @field_validator("goal")
    @classmethod
    def _non_empty_goal(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("goal must be a non-empty string.")
        return v

    @model_validator(mode="after")
    def _validate_expected_outputs(self) -> "WorkSpec":
        if not self.expected_outputs:
            raise ValueError("expected_outputs must be a non-empty list.")
        return self

    def to_public_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
