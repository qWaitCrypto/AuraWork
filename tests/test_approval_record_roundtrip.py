from __future__ import annotations

from aura.runtime.approval import ApprovalRecord, ApprovalStatus


def test_approval_record_roundtrip_preserves_fields() -> None:
    original = ApprovalRecord(
        approval_id="apr_1",
        session_id="sess_1",
        request_id="req_1",
        created_at=1_700_000_000_000,
        status=ApprovalStatus.GRANTED,
        turn_id="turn_1",
        action_summary="Run shell command",
        risk_level="high",
        options=["approve", "deny", "edit", "dry_run"],
        reason="Needs confirmation",
        diff_ref={"locator": "artifact://diff"},
        resume_kind="tool_call",
        resume_payload={"tool_name": "shell__run", "arguments": {"cmd": "echo ok"}},
        decision={"decision": "approve", "note": "looks safe"},
    )

    raw = original.to_dict()
    restored = ApprovalRecord.from_dict(raw)
    assert restored == original


def test_approval_record_from_dict_falls_back_on_invalid_status() -> None:
    raw = {
        "approval_id": "apr_2",
        "session_id": "sess_2",
        "request_id": "req_2",
        "created_at": 1_700_000_000_123,
        "status": "unexpected_status",
        "action_summary": "",
        "options": [],
        "resume_payload": {},
    }
    restored = ApprovalRecord.from_dict(raw)
    assert restored.status is ApprovalStatus.PENDING
