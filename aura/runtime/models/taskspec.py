from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class WorkerArchetype(StrEnum):
    FILE_OPS_WORKER = "FileOpsWorker"
    DOC_WORKER = "DocWorker"
    SHEET_WORKER = "SheetWorker"
    WEB_READ_WORKER = "WebReadWorker"
    VERIFIER_WORKER = "VerifierWorker"


class ArtifactRef(BaseModel):
    artifact_id: str | None = None
    path: str | None = None


class AcceptanceTestType(StrEnum):
    EXISTS = "exists"
    FORMAT_VALID = "format_valid"
    FIELD_COMPLETE = "field_complete"
    CHECKSUM = "checksum"
    CUSTOM = "custom"


class AcceptanceTest(BaseModel):
    type: AcceptanceTestType | None = None
    target: str | None = None
    params: dict[str, Any] | None = None


class TaskSpec(BaseModel):
    """TaskSpec data model per design doc §11.2."""

    id: str
    goal: str
    deps: list[str] | None = None
    inputs: list[ArtifactRef] | None = None
    outputs: list[ArtifactRef] | None = None
    worker_archetype: WorkerArchetype
    allowed_tools_subset: list[str] | None = None
    risk_level: int | None = Field(default=None, ge=0, le=4)
    acceptance_tests: list[AcceptanceTest] | None = None
    read_set: list[str] | None = None
    write_set: list[str] | None = None

    @field_validator("id", "goal")
    @classmethod
    def _non_empty_str(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Must be a non-empty string.")
        return v
