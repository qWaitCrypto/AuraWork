from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

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

    def _run_sync() -> dict[str, Any]:
        return subagent_tool.execute(
            args={
                "preset": preset_name,
                "task": enhanced_task,
                "context": {"text": f"Node ID: {node.id}"},
                "work_spec": work_spec,
            },
            project_root=project_root,
            context=context,
        )

    try:
        result = await asyncio.to_thread(_run_sync)
    except Exception as exc:
        return NodeDispatchResult(node_id=node.id, status="error", result=None, error=str(exc))

    status = str(result.get("status") or "unknown")
    status_norm = status.strip().lower()
    report = _parse_report(result)
    report_status_norm = str(report.get("status") or "").strip().lower()
    # Text phrases an LLM may emit when it wants approval but didn't use the
    # structured field.  We check the full serialized report so we catch prose
    # buried in nested fields (e.g. "proposals", "receipts", "error" text).
    _APPROVAL_TEXT_SIGNALS = (
        "need confirmation",
        "need your approval",
        "need approval",
        "needs your approval",
        "awaiting approval",
        "please approve",
        "require confirmation",
        "requires confirmation",
        "waiting for confirmation",
        "waiting for approval",
        "requesting approval",
    )
    _report_text = json.dumps(report).lower() if report else ""

    if (
        result.get("needs_approval")
        or report.get("needs_approval")
        or report_status_norm in {"needs_approval", "require_approval", "requires_approval", "pending_approval"}
        or any(sig in _report_text for sig in _APPROVAL_TEXT_SIGNALS)
    ):
        status = "needs_approval"
    elif status_norm in {"needs_user_takeover", "user_takeover_required", "needs_takeover"} or report_status_norm in {
        "needs_user_takeover",
        "user_takeover_required",
        "needs_takeover",
    }:
        status = "needs_user_takeover"
    elif result.get("error") or status_norm == "failed" or report_status_norm == "failed":
        status = "failed"

    # If the subagent reported "completed" but executed zero tool calls, demote to
    # "failed".  Any worker that meaningfully produces a deliverable (doc, sheet,
    # research) must call at least one tool.  Zero receipts means the LLM either
    # hallucinated success or bailed out without doing real work.  This catches
    # both the "error signals in report" case and the silent false-completion case
    # (LLM emits {"status":"completed","receipts":[]} with no error text).
    if status not in {"failed", "needs_approval", "needs_user_takeover"}:
        all_receipts = _extract_receipts(result)
        if len(all_receipts) == 0:
            status = "failed"

    return NodeDispatchResult(node_id=node.id, status=status, result=result, error=None)


def _parse_report(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {}

    report = result.get("report")
    if report is None:
        return {}
    if isinstance(report, dict):
        return report
    if isinstance(report, str):
        try:
            parsed = json.loads(report)
        except (json.JSONDecodeError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _extract_proposals(report: dict[str, Any]) -> list[dict[str, Any]]:
    proposals = report.get("proposals")
    if isinstance(proposals, list):
        return [p for p in proposals if isinstance(p, dict)]
    return []


def _extract_artifacts(report: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts = report.get("artifacts")
    if isinstance(artifacts, list):
        return [a for a in artifacts if isinstance(a, dict)]
    return []


def _extract_receipts(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    if result is None:
        return []

    receipts = result.get("receipts")
    if isinstance(receipts, list):
        return [r for r in receipts if isinstance(r, dict)]

    report = _parse_report(result)
    receipts = report.get("receipts")
    if isinstance(receipts, list):
        return [r for r in receipts if isinstance(r, dict)]
    return []


def _non_empty_str(value: Any) -> str | None:
    if isinstance(value, str):
        s = value.strip()
        if s:
            return s
    return None


def _extract_failure_message(*, result: dict[str, Any] | None, report: dict[str, Any]) -> str:
    direct_error = None
    if isinstance(result, dict):
        direct_error = _non_empty_str(result.get("error"))
    if direct_error is None:
        direct_error = _non_empty_str(report.get("error"))
    if direct_error is not None:
        return direct_error

    error_code = None
    summary = None
    if isinstance(result, dict):
        error_code = _non_empty_str(result.get("error_code"))
        summary = _non_empty_str(result.get("summary") or result.get("message"))

    if error_code is None:
        error_code = _non_empty_str(report.get("error_code"))
    if summary is None:
        summary = _non_empty_str(report.get("summary") or report.get("message"))

    if error_code and summary:
        return f"{error_code}: {summary}"
    if summary:
        return summary
    if error_code:
        return error_code
    return "Unknown failure"


def _extract_takeover_request(*, result: dict[str, Any] | None, report: dict[str, Any]) -> dict[str, Any]:
    next_step = report.get("next_step_suggestion") if isinstance(report.get("next_step_suggestion"), dict) else {}

    reason = str(
        report.get("reason")
        or next_step.get("reason")
        or next_step.get("message")
        or "Browser task requires user takeover (CAPTCHA/login/2FA)."
    ).strip()
    action_summary = str(
        report.get("action_summary")
        or next_step.get("action_summary")
        or "Please complete browser verification/login, then approve to resume."
    ).strip()

    out: dict[str, Any] = {
        "kind": "user_takeover",
        "mode": "user_takeover",
        "action_summary": action_summary,
        "risk_level": "medium",
        "reason": reason,
        "options": ["approve", "deny"],
        "status": "needs_user_takeover",
    }

    current_url = report.get("current_url") or next_step.get("current_url")
    if isinstance(current_url, str) and current_url.strip():
        out["current_url"] = current_url.strip()

    screenshot = report.get("screenshot") or next_step.get("screenshot")
    if isinstance(screenshot, str) and screenshot.strip():
        out["screenshot"] = screenshot.strip()

    next_hint = report.get("next_step") or next_step.get("next_step")
    if isinstance(next_hint, str) and next_hint.strip():
        out["next_step"] = next_hint.strip()

    if isinstance(result, dict):
        run_id = result.get("subagent_run_id")
        if isinstance(run_id, str) and run_id.strip():
            out["subagent_run_id"] = run_id.strip()

        browser_session = result.get("browser_agent_session")
        if isinstance(browser_session, str) and browser_session.strip():
            out["browser_agent_session"] = browser_session.strip()

    if "browser_agent_session" not in out:
        browser_session_report = report.get("browser_agent_session") if isinstance(report, dict) else None
        if isinstance(browser_session_report, str) and browser_session_report.strip():
            out["browser_agent_session"] = browser_session_report.strip()

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

        result = dispatch_result.result
        report = _parse_report(result)
        proposals = tuple(_extract_proposals(report))
        artifacts = tuple(_extract_artifacts(report))
        receipts = tuple(_extract_receipts(result))

        if dispatch_result.status == "needs_approval":
            approval_request: dict[str, Any] | None = None
            if result is not None:
                approval_raw = result.get("needs_approval")
                if approval_raw is None:
                    approval_raw = report.get("needs_approval")
                if isinstance(approval_raw, dict):
                    approval_request = approval_raw
                elif isinstance(approval_raw, list):
                    if len(approval_raw) == 1 and isinstance(approval_raw[0], dict):
                        approval_request = approval_raw[0]
                    else:
                        approval_request = {"requests": [r for r in approval_raw if isinstance(r, dict)]}
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
            approval_request = _extract_takeover_request(result=result, report=report)
            return NodeCompletionAction(
                action="pause_for_approval",
                node_id=node_id,
                proposals=proposals,
                approval_request=approval_request,
                artifacts=artifacts,
                receipts=receipts,
            )

        if dispatch_result.status == "failed":
            error_msg = _extract_failure_message(result=result, report=report)
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
