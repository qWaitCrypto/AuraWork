from __future__ import annotations

from aura.runtime.subagents.approval_manager import ApprovalManager


def test_resolve_pending_approval_sets_event_and_decision() -> None:
    manager = ApprovalManager()
    pending = manager.register("appr-1", {"tool_name": "shell__run"})

    assert manager.resolve("appr-1", "approve") is True
    assert pending.event.wait(timeout=0.2) is True
    assert pending.decision[0] == "approve"


def test_deny_all_releases_waiters_and_clears_registry() -> None:
    manager = ApprovalManager()
    pending = manager.register("appr-2", {"tool_name": "browser__run"})

    manager.deny_all()

    assert pending.event.wait(timeout=0.2) is True
    assert pending.decision[0] == "deny"
    assert manager.list_pending() == []


def test_register_after_deny_all_is_immediately_denied_until_reopen() -> None:
    manager = ApprovalManager()
    manager.deny_all()

    pending_closed = manager.register("appr-3", {"tool_name": "shell__run"})
    assert pending_closed.event.wait(timeout=0.2) is True
    assert pending_closed.decision[0] == "deny"
    assert manager.list_pending() == []

    manager.reopen()
    pending_open = manager.register("appr-4", {"tool_name": "shell__run"})
    assert pending_open.event.is_set() is False
    assert pending_open.decision[0] is None
    assert manager.resolve("appr-4", "approve") is True
    assert manager.resolve("appr-4", "deny") is False
