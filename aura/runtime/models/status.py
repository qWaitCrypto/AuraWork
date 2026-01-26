from __future__ import annotations

from enum import StrEnum
from typing import Iterable


class RunStatus(StrEnum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    STAGING = "STAGING"
    VERIFYING = "VERIFYING"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SKIPPED = "SKIPPED"


class ApprovalStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"


_RUN_TERMINAL: frozenset[RunStatus] = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})
_NODE_TERMINAL: frozenset[NodeStatus] = frozenset(
    {NodeStatus.APPLIED, NodeStatus.FAILED, NodeStatus.ROLLED_BACK, NodeStatus.SKIPPED}
)
_APPROVAL_TERMINAL: frozenset[ApprovalStatus] = frozenset(
    {ApprovalStatus.NOT_REQUIRED, ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.TIMEOUT}
)


_ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.PLANNING, RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.PLANNING: frozenset({RunStatus.EXECUTING, RunStatus.CANCELLED, RunStatus.FAILED}),
    # Allow re-plan during execution (e.g. expansion) and move to verification.
    RunStatus.EXECUTING: frozenset({RunStatus.PLANNING, RunStatus.VERIFYING, RunStatus.CANCELLED, RunStatus.FAILED}),
    # Allow looping back to execution (more nodes, retry) or completion.
    RunStatus.VERIFYING: frozenset({RunStatus.EXECUTING, RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


_ALLOWED_NODE_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PENDING: frozenset({NodeStatus.READY, NodeStatus.SKIPPED, NodeStatus.FAILED}),
    NodeStatus.READY: frozenset({NodeStatus.RUNNING, NodeStatus.SKIPPED, NodeStatus.FAILED}),
    NodeStatus.RUNNING: frozenset({NodeStatus.STAGING, NodeStatus.FAILED}),
    # After staging, verification may be required, or a node may go directly to approval or applied.
    NodeStatus.STAGING: frozenset({NodeStatus.VERIFYING, NodeStatus.APPROVAL_PENDING, NodeStatus.APPLIED, NodeStatus.FAILED}),
    NodeStatus.VERIFYING: frozenset({NodeStatus.APPROVAL_PENDING, NodeStatus.APPLIED, NodeStatus.FAILED}),
    # Approval can apply, or reject/rollback.
    NodeStatus.APPROVAL_PENDING: frozenset({NodeStatus.APPLIED, NodeStatus.ROLLED_BACK, NodeStatus.FAILED}),
    NodeStatus.APPLIED: frozenset(),
    NodeStatus.FAILED: frozenset(),
    NodeStatus.ROLLED_BACK: frozenset(),
    NodeStatus.SKIPPED: frozenset(),
}


_ALLOWED_APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.NOT_REQUIRED: frozenset(),
    ApprovalStatus.PENDING: frozenset({ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.TIMEOUT}),
    ApprovalStatus.APPROVED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.TIMEOUT: frozenset(),
}


def is_terminal_status(status: RunStatus | NodeStatus | ApprovalStatus) -> bool:
    if isinstance(status, RunStatus):
        return status in _RUN_TERMINAL
    if isinstance(status, NodeStatus):
        return status in _NODE_TERMINAL
    return status in _APPROVAL_TERMINAL


def allowed_next_run_statuses(status: RunStatus) -> frozenset[RunStatus]:
    return _ALLOWED_RUN_TRANSITIONS.get(status, frozenset())


def allowed_next_node_statuses(status: NodeStatus) -> frozenset[NodeStatus]:
    return _ALLOWED_NODE_TRANSITIONS.get(status, frozenset())


def allowed_next_approval_statuses(status: ApprovalStatus) -> frozenset[ApprovalStatus]:
    return _ALLOWED_APPROVAL_TRANSITIONS.get(status, frozenset())


def validate_run_transition(*, before: RunStatus, after: RunStatus) -> None:
    _validate_transition(before=before, after=after, allowed=allowed_next_run_statuses(before), kind="RunStatus")


def validate_node_transition(*, before: NodeStatus, after: NodeStatus) -> None:
    _validate_transition(before=before, after=after, allowed=allowed_next_node_statuses(before), kind="NodeStatus")


def validate_approval_transition(*, before: ApprovalStatus, after: ApprovalStatus) -> None:
    _validate_transition(
        before=before,
        after=after,
        allowed=allowed_next_approval_statuses(before),
        kind="ApprovalStatus",
    )


def _validate_transition(*, before: StrEnum, after: StrEnum, allowed: Iterable[StrEnum], kind: str) -> None:
    allowed_set = set(allowed)
    if after in allowed_set:
        return
    if before == after:
        raise ValueError(f"Illegal {kind} transition: {before.value} -> {after.value} (no-op not allowed)")
    rendered = ", ".join(s.value for s in sorted(allowed_set, key=lambda s: s.value))
    raise ValueError(f"Illegal {kind} transition: {before.value} -> {after.value} (allowed: {rendered or '∅'})")
