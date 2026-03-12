from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from aura.runtime.models import ApprovalRequest, SubagentReceipt, SubagentResult
from aura.runtime.parallel_dispatch import NodeCompletionHandler, dispatch_nodes_parallel
from aura.runtime.plan import PlanItem, StepStatus


class _DummySubagentTool:
    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def execute(self, *, args: dict[str, Any], project_root, context=None) -> dict[str, Any]:
        _ = args
        _ = project_root
        _ = context
        return dict(self._result)


def test_dispatch_invalid_subagent_result_schema_marks_failed() -> None:
    node = PlanItem(id="n1", step="do work", status=StepStatus.PENDING, depends_on=[])
    tool = _DummySubagentTool(result={"foo": "bar"})

    results = asyncio.run(
        dispatch_nodes_parallel(
            nodes=[node],
            subagent_tool=tool,
            preset_selector=lambda _n: "file_ops_worker",
            work_spec_selector=lambda _n: {},
            project_root=Path(".").resolve(),
        )
    )

    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].error == "Invalid subagent result schema."


def test_dispatch_completed_without_receipts_demotes_to_failed() -> None:
    node = PlanItem(id="n1", step="do work", status=StepStatus.PENDING, depends_on=[])
    tool = _DummySubagentTool(result={"status": "completed", "receipts": []})

    results = asyncio.run(
        dispatch_nodes_parallel(
            nodes=[node],
            subagent_tool=tool,
            preset_selector=lambda _n: "file_ops_worker",
            work_spec_selector=lambda _n: {},
            project_root=Path(".").resolve(),
        )
    )

    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].error == "Subagent reported completion without receipts."


def test_completion_handler_reads_typed_approval_request() -> None:
    typed = SubagentResult(
        status="needs_approval",
        receipts=[SubagentReceipt(tool="shell__run", args_summary="echo", result_summary="pending")],
        approval_request=ApprovalRequest(
            action_summary="Run shell command",
            risk_level="high",
            reason="requires approval",
            tool_name="shell__run",
        ),
    ).model_dump()

    from aura.runtime.parallel_dispatch import NodeDispatchResult

    action = NodeCompletionHandler().process_dispatch_result(
        NodeDispatchResult(node_id="n1", status="needs_approval", result=typed, error=None)
    )

    assert action.action == "pause_for_approval"
    assert isinstance(action.approval_request, dict)
    assert action.approval_request.get("tool_name") == "shell__run"
