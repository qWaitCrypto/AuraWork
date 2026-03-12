from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SubagentReceipt(BaseModel):
    tool: str
    args_summary: str = ""
    result_summary: str = ""


class SubagentArtifact(BaseModel):
    type: str
    path: str
    format: str | None = None


class ApprovalRequest(BaseModel):
    kind: str = "tool_approval"
    action_summary: str
    risk_level: str = "medium"
    reason: str = ""
    tool_name: str | None = None


class SubagentResult(BaseModel):
    status: Literal["completed", "failed", "needs_approval", "needs_user_takeover"]
    receipts: list[SubagentReceipt] = Field(default_factory=list)
    artifacts: list[SubagentArtifact] = Field(default_factory=list)
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    approval_request: ApprovalRequest | None = None
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

