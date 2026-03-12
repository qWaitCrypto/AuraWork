from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingApproval:
    approval_id: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: list[str | None] = field(default_factory=lambda: [None])
    request: dict[str, Any] = field(default_factory=dict)


class ApprovalManager:
    """Session-scoped registry for in-flight tool approvals."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()
        self._closed = False

    def register(self, approval_id: str, request: dict[str, Any]) -> PendingApproval:
        with self._lock:
            pending = PendingApproval(approval_id=approval_id, request=dict(request))
            if self._closed:
                pending.decision[0] = "deny"
                pending.event.set()
                return pending
            self._pending[approval_id] = pending
        return pending

    def resolve(self, approval_id: str, decision: str) -> bool:
        with self._lock:
            if self._closed:
                return False
            pending = self._pending.get(approval_id)
            if pending is None:
                return False
            if pending.decision[0] is not None:
                return False
            pending.decision[0] = str(decision or "").strip().lower() or None
            pending.event.set()
            return True

    def unregister(self, approval_id: str) -> None:
        with self._lock:
            self._pending.pop(approval_id, None)

    def deny_all(self) -> None:
        with self._lock:
            self._closed = True
            pending_items = list(self._pending.values())
            self._pending.clear()
        for pending in pending_items:
            if pending.decision[0] is None:
                pending.decision[0] = "deny"
            pending.event.set()

    def reopen(self) -> None:
        with self._lock:
            self._closed = False

    def list_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._pending.values())
        out: list[dict[str, Any]] = []
        for pending in items:
            row = {"approval_id": pending.approval_id}
            row.update(dict(pending.request))
            out.append(row)
        return out
