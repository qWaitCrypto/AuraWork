from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from ..audit_store import AuditStore
from ..models.audit_event import AuditEvent, AuditEventType
from ..snapshots import GitSnapshotBackend


class TransactionState(StrEnum):
    """Transaction lifecycle state for a single node execution."""

    CREATED = "created"
    EXECUTING = "executing"
    STAGED = "staged"
    APPLIED = "applied"
    DISCARDED = "discarded"


@dataclass(slots=True)
class NodeTransaction:
    """
    Node execution transaction (design doc §10.3).

    This is a light connector between:
    - Shadow Git worktrees (GitSnapshotBackend)
    - Node execution (subagent / tools; executed elsewhere)

    Lifecycle:
    - begin(): create worktree
    - commit(): commit changes in the worktree branch
    - apply(): mark applied (merge/apply is intentionally deferred)
    - discard(): remove worktree and mark discarded
    """

    backend: GitSnapshotBackend
    task_id: str
    node_id: str
    audit_store: AuditStore | None = None
    run_id: str | None = None
    state: TransactionState = TransactionState.CREATED

    _worktree_path: Path | None = field(default=None, init=False, repr=False)
    _commit_hash: str | None = field(default=None, init=False, repr=False)

    def begin(self) -> Path:
        if self.state is not TransactionState.CREATED:
            raise RuntimeError(f"Cannot begin transaction in state {self.state}.")
        self._worktree_path = self.backend.create_worktree(task_id=self.task_id, node_id=self.node_id)
        self.state = TransactionState.EXECUTING
        self._audit(AuditEventType.NODE_STARTED, {"worktree_path": str(self._worktree_path)})
        return self._worktree_path

    def commit(self, message: str) -> str:
        if self.state is not TransactionState.EXECUTING:
            raise RuntimeError(f"Cannot commit transaction in state {self.state}.")
        self._commit_hash = self.backend.worktree_commit(task_id=self.task_id, node_id=self.node_id, message=message)
        self.state = TransactionState.STAGED
        self._audit(AuditEventType.CHANGESET_READY, {"commit": self._commit_hash, "message": message})
        return self._commit_hash

    def apply(self) -> None:
        if self.state is not TransactionState.STAGED:
            raise RuntimeError(f"Cannot apply transaction in state {self.state}.")
        # Simplified connector layer:
        # - worktree changes are committed in the shadow repo
        # - merge/apply to the main workspace is intentionally deferred to later iterations
        self.state = TransactionState.APPLIED
        self._audit(AuditEventType.APPLIED_TO_MAINLINE, {"commit": self._commit_hash})

    def discard(self) -> None:
        if self.state is TransactionState.DISCARDED:
            return
        self.backend.remove_worktree(task_id=self.task_id, node_id=self.node_id)
        self.state = TransactionState.DISCARDED

    @property
    def worktree_path(self) -> Path | None:
        return self._worktree_path

    @property
    def commit_hash(self) -> str | None:
        return self._commit_hash

    def _audit(self, event_type: AuditEventType, payload: dict[str, object]) -> None:
        if self.audit_store is None:
            return
        evt = AuditEvent(
            event_id=uuid4(),
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
            run_id=self.run_id,
            task_id=self.task_id,
            node_id=self.node_id,
            payload=dict(payload),
        )
        self.audit_store.append(evt)
