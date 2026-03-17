from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from .models import SubagentResult
from .plan import PlanItem
from .tools.runtime import ToolExecutionContext

if TYPE_CHECKING:
    from .tools.subagent_runner import SubagentRunTool


@dataclass(frozen=True, slots=True)
class NodeDispatchResult:
    """Dispatch outcome for a single PlanItem node."""

    node_id: str
    status: str
    result: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class NodeCompletionAction:
    """Action to take after a node finishes."""

    action: str
    node_id: str
    proposals: tuple[dict[str, Any], ...] = ()
    approval_request: dict[str, Any] | None = None
    error: str | None = None
    artifacts: tuple[dict[str, Any], ...] = ()
    receipts: tuple[dict[str, Any], ...] = ()


_INVALID_SUBAGENT_RESULT_SCHEMA = "Invalid subagent result schema."


def _normalize_status_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().lower()
    if not raw:
        return None
    if raw in {"completed", "complete", "done", "ok", "success", "succeeded"}:
        return "completed"
    if raw in {"failed", "fail", "error", "errored", "failure"}:
        return "failed"
    if raw in {"needs_approval", "require_approval", "requires_approval", "pending_approval"}:
        return "needs_approval"
    if raw in {"needs_user_takeover", "user_takeover_required", "needs_takeover"}:
        return "needs_user_takeover"
    return None


def _status_from_report(report: Any) -> str | None:
    if not isinstance(report, dict):
        return None
    return _normalize_status_value(report.get("status"))


def _resolve_status_from_report(*, current_status: str, typed: SubagentResult) -> tuple[str, str | None]:
    report_status = _status_from_report(typed.report)
    if report_status is None or report_status == current_status:
        return current_status, None

    if report_status == "needs_approval" and typed.approval_request is None:
        return "failed", "Subagent status/report mismatch: needs_approval without approval_request."

    if current_status in {"needs_approval", "needs_user_takeover"} and report_status == "completed":
        return current_status, None

    return report_status, None


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


_SUBAGENT_DISPATCH_MAX_WORKERS = _env_int("AURA_SUBAGENT_DISPATCH_MAX_WORKERS", 32, minimum=4, maximum=256)
_SUBAGENT_DISPATCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=_SUBAGENT_DISPATCH_MAX_WORKERS,
    thread_name_prefix="aura-subagent-dispatch",
)


async def _dispatch_single_node(
    *,
    node: PlanItem,
    subagent_tool: SubagentRunTool,
    preset_name: str,
    work_spec: dict[str, Any],
    project_root: Path,
    context: ToolExecutionContext | None,
    goal: str | None = None,
    progress_summary: str | None = None,
) -> NodeDispatchResult:
    # Build an enhanced task description by injecting global goal and progress.
    if goal or progress_summary:
        enhanced_task = ""
        if goal:
            enhanced_task += f"## Global Goal\n{goal}\n\n"
        if progress_summary:
            enhanced_task += f"## Current Progress\n{progress_summary}\n\n"
        enhanced_task += f"## Current Task\n{node.step}"
    else:
        enhanced_task = node.step

    loop = asyncio.get_running_loop()
    enriched_context = dataclasses.replace(context, event_loop=loop) if context is not None else None

    def _run_sync() -> dict[str, Any]:
        return subagent_tool.execute(
            args={
                "preset": preset_name,
                "task": enhanced_task,
                "context": {"text": f"Node ID: {node.id}"},
                "work_spec": work_spec,
            },
            project_root=project_root,
            context=enriched_context,
        )

    try:
        raw_result = await loop.run_in_executor(_SUBAGENT_DISPATCH_EXECUTOR, _run_sync)
    except Exception as exc:
        return NodeDispatchResult(node_id=node.id, status="error", result=None, error=str(exc))

    if not isinstance(raw_result, dict):
        return NodeDispatchResult(
            node_id=node.id,
            status="failed",
            result=None,
            error=_INVALID_SUBAGENT_RESULT_SCHEMA,
        )

    try:
        typed = SubagentResult.model_validate(raw_result)
    except ValidationError:
        return NodeDispatchResult(
            node_id=node.id,
            status="failed",
            result=raw_result,
            error=_INVALID_SUBAGENT_RESULT_SCHEMA,
        )

    status = typed.status
    status, error = _resolve_status_from_report(current_status=status, typed=typed)
    if status == "completed" and len(typed.receipts) == 0:
        status = "failed"
        error = "Subagent reported completion without receipts."

    return NodeDispatchResult(node_id=node.id, status=status, result=typed.model_dump(), error=error)


def _parse_subagent_result(result: dict[str, Any] | None) -> SubagentResult | None:
    if result is None:
        return None
    try:
        return SubagentResult.model_validate(result)
    except ValidationError:
        return None


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        s = value.strip()
        if s:
            return s
    return None


def _extract_failure_message(*, typed: SubagentResult, fallback_error: str | None = None) -> str:
    if isinstance(fallback_error, str) and fallback_error.strip():
        return fallback_error.strip()

    direct_error = _non_empty_str(typed.error)
    if direct_error is not None:
        return direct_error

    report = typed.report if isinstance(typed.report, dict) else {}
    if isinstance(report, dict):
        report_error = _non_empty_str(report.get("error") or report.get("summary") or report.get("message"))
        if report_error is not None:
            return report_error

    data = typed.data if isinstance(typed.data, dict) else {}
    error_code = _non_empty_str(data.get("error_code"))
    summary = _non_empty_str(data.get("summary") or data.get("message"))
    if error_code and summary:
        return f"{error_code}: {summary}"
    if summary:
        return summary
    if error_code:
        return error_code
    return "Unknown failure"


def _extract_takeover_request(*, typed: SubagentResult) -> dict[str, Any]:
    data = typed.data if isinstance(typed.data, dict) else {}

    reason = (
        _non_empty_str(data.get("reason"))
        or _non_empty_str(typed.error)
        or "Browser task requires user takeover (CAPTCHA/login/2FA)."
    )
    action_summary = (
        _non_empty_str(data.get("action_summary"))
        or "Please complete browser verification/login, then approve to resume."
    )

    out: dict[str, Any] = {
        "kind": "user_takeover",
        "mode": "user_takeover",
        "action_summary": action_summary,
        "risk_level": "medium",
        "reason": reason,
        "options": ["approve", "deny"],
        "status": "needs_user_takeover",
    }

    for key in ("current_url", "screenshot", "next_step", "subagent_run_id", "browser_agent_session"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()

    return out


class NodeCompletionHandler:
    """Process dispatch results into completion actions."""

    def process_dispatch_result(self, dispatch_result: NodeDispatchResult) -> NodeCompletionAction:
        node_id = dispatch_result.node_id

        if dispatch_result.status == "error":
            return NodeCompletionAction(
                action="mark_error",
                node_id=node_id,
                error=dispatch_result.error,
            )

        typed = _parse_subagent_result(dispatch_result.result)
        if typed is None:
            return NodeCompletionAction(
                action="mark_failed",
                node_id=node_id,
                error=dispatch_result.error or _INVALID_SUBAGENT_RESULT_SCHEMA,
            )

        proposals = tuple(dict(p) for p in typed.proposals if isinstance(p, dict))
        artifacts = tuple(a.model_dump() for a in typed.artifacts)
        receipts = tuple(r.model_dump() for r in typed.receipts)
        approval_request = typed.approval_request.model_dump() if typed.approval_request is not None else None

        if dispatch_result.status == "needs_approval":
            if approval_request is None:
                approval_request = {"reason": "Subagent requested approval"}

            return NodeCompletionAction(
                action="pause_for_approval",
                node_id=node_id,
                proposals=proposals,
                approval_request=approval_request,
                artifacts=artifacts,
                receipts=receipts,
            )

        if dispatch_result.status == "needs_user_takeover":
            approval_request = approval_request or _extract_takeover_request(typed=typed)
            return NodeCompletionAction(
                action="pause_for_approval",
                node_id=node_id,
                proposals=proposals,
                approval_request=approval_request,
                artifacts=artifacts,
                receipts=receipts,
            )

        if dispatch_result.status == "failed":
            error_msg = _extract_failure_message(typed=typed, fallback_error=dispatch_result.error)
            return NodeCompletionAction(
                action="mark_failed",
                node_id=node_id,
                proposals=proposals,
                error=str(error_msg),
                artifacts=artifacts,
                receipts=receipts,
            )

        return NodeCompletionAction(
            action="mark_completed",
            node_id=node_id,
            proposals=proposals,
            artifacts=artifacts,
            receipts=receipts,
        )

    def process_batch(self, results: list[NodeDispatchResult]) -> list[NodeCompletionAction]:
        return [self.process_dispatch_result(r) for r in results]

    def aggregate_proposals(self, actions: list[NodeCompletionAction]) -> list[dict[str, Any]]:
        all_proposals: list[dict[str, Any]] = []
        for action in actions:
            for proposal in action.proposals:
                tagged = dict(proposal)
                tagged["from_node"] = action.node_id
                all_proposals.append(tagged)
        return all_proposals


async def dispatch_nodes_parallel(
    *,
    nodes: list[PlanItem],
    subagent_tool: SubagentRunTool,
    preset_selector: Callable[[PlanItem], str],
    work_spec_selector: Callable[[PlanItem], dict[str, Any]],
    project_root: Path,
    context: ToolExecutionContext | None = None,
    goal: str | None = None,
    progress_summary: str | None = None,
) -> list[NodeDispatchResult]:
    """
    Dispatch PlanItem nodes to subagents concurrently.
    
    Args:
        goal: Global goal injected into each subagent context.
        progress_summary: Progress summary injected into each subagent context.
    """

    if not nodes:
        return []

    tasks = [
        asyncio.create_task(
            _dispatch_single_node(
                node=node,
                subagent_tool=subagent_tool,
                preset_name=preset_selector(node),
                work_spec=work_spec_selector(node),
                project_root=project_root,
                context=context,
                goal=goal,
                progress_summary=progress_summary,
            )
        )
        for node in nodes
    ]

    async def _heartbeat() -> None:
        # Keep the loop ticking while thread-based tasks finish.
        while True:
            if all(task.done() for task in tasks):
                return
            await asyncio.sleep(0.05)

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        results = await asyncio.gather(*tasks, return_exceptions=False)
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

    return list(results)


def default_preset_selector(node: PlanItem) -> str:
    """
    Basic preset selector based on the node step content.
    """

    step_lower = node.step.lower()

    if any(
        kw in step_lower
        for kw in ["verify", "check", "validate", "\u9a8c\u8bc1", "\u68c0\u67e5", "\u5bf9\u8d26"]
    ):
        return "verifier"
    if any(
        kw in step_lower
        for kw in [
            "scan",
            "list",
            "move",
            "delete",
            "rename",
            "copy",
            "\u626b\u63cf",
            "\u79fb\u52a8",
            "\u5220\u9664",
            "\u91cd\u547d\u540d",
            "\u5f52\u6863",
        ]
    ):
        return "file_ops_worker"
    if any(
        kw in step_lower
        for kw in ["fetch", "search", "web", "\u7f51\u9875", "\u641c\u7d22", "\u6293\u53d6", "\u8c03\u7814"]
    ):
        return "browser_worker"
    if any(
        kw in step_lower
        for kw in ["document", "report", "write", "\u6587\u6863", "\u62a5\u544a", "\u64b0\u5199", "\u751f\u6210"]
    ):
        return "doc_worker"
    if any(kw in step_lower for kw in ["sheet", "excel", "csv", "\u8868\u683c", "\u6570\u636e\u6e05\u6d17"]):
        return "sheet_worker"

    # Default: general-purpose executor with safe project tool access.
    return "file_ops_worker"


__all__ = [
    "NodeDispatchResult",
    "NodeCompletionAction",
    "NodeCompletionHandler",
    "dispatch_nodes_parallel",
    "default_preset_selector",
]
