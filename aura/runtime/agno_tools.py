from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from .ids import new_id, new_tool_call_id
from .llm.types import CanonicalMessage, CanonicalMessageRole, ToolSpec
from .orchestrator_helpers import _summarize_tool_for_ui
from .protocol import EventKind
from .tools.runtime import (
    InspectionDecision,
    PlannedToolCall,
    ToolExecutionContext,
    ToolRuntime,
    _classify_tool_exception,
    _classify_tool_result,
    _normalize_tool_end_status,
 )


@dataclass(frozen=True, slots=True)
class AgnoToolset:
    """
    A bundle of agno Function objects plus a small amount of metadata.

    We keep this wrapper lightweight so the rest of Aura doesn't need to import
    agno types at module import time.
    """

    functions: list[Any]


_STANDARD_CONFIRM_TOOL_NAMES: set[str] = {
    # Export writes files into the project.
    "session__export",
    # Shell can mutate state / exfiltrate.
    "shell__run",
    # Browser automation can change remote state depending on the command; gate via ToolRuntime inspection.
    "browser__run",
    # File edits mutate the project working tree; gate via ToolRuntime inspection.
    "project__apply_patch",
    "project__apply_edits",
    "project__patch",
    # Spec workflow is always approval-gated.
    "spec__apply",
    "spec__seal",
    # Rollback overwrites the working tree; always approval-gated.
    "snapshot__rollback",
}


def _should_require_confirmation(*, tool_name: str, approval_mode: Any) -> bool:
    """
    Decide whether an Aura tool should be marked `requires_confirmation` for agno.

    We intentionally over-approximate (pause early) and then apply Aura's
    argument-sensitive policy in the engine when the run is paused.
    """

    mode_value = getattr(approval_mode, "value", approval_mode)
    if mode_value == "strict":
        return True
    if tool_name in _STANDARD_CONFIRM_TOOL_NAMES:
        return True
    return False


def build_agno_toolset(
    *,
    tool_specs: list[ToolSpec],
    tool_runtime: ToolRuntime,
    emit: Callable[..., Any],
    append_history: Callable[[CanonicalMessage], None],
    event_bus: Any | None = None,
    tool_call_budget: int | None = None,
) -> AgnoToolset:
    """
    Wrap Aura tools as agno Functions (execution happens inside agno).

    The wrappers:
    - Execute the underlying tool implementation directly (ToolRuntime is used only for policy/inspection).
    - Emit Aura TOOL_CALL_* events.
    - Append TOOL messages into Aura history to keep future context consistent.
    """

    try:
        from agno.tools.function import Function as AgnoFunction
    except Exception as e:  # pragma: no cover - optional dependency
        raise RuntimeError(f"Agno tools unavailable: {e}") from e

    approval_mode = tool_runtime.get_approval_mode()
    artifact_store = tool_runtime.artifact_store
    tool_calls_used = 0

    def _consume_tool_call_budget() -> int | None:
        nonlocal tool_calls_used
        if tool_call_budget is None:
            return None
        tool_calls_used += 1
        return max(0, int(tool_call_budget) - tool_calls_used)

    functions: list[Any] = []
    for spec in tool_specs:
        def _make_entrypoint(bound_tool_name: str) -> Callable[..., str]:
            def _entrypoint(*, run_context: Any | None = None, fc: Any | None = None, **kwargs: Any) -> str:
                meta = getattr(run_context, "metadata", None) or {}
                subagent_run_id = meta.get("aura_subagent_run_id")
                subagent_preset = meta.get("aura_subagent_preset")
                browser_agent_session = meta.get("aura_browser_agent_session")

                def _with_subagent_meta(payload: dict[str, Any]) -> dict[str, Any]:
                    out = dict(payload)
                    if isinstance(subagent_run_id, str) and subagent_run_id:
                        out.setdefault("subagent_run_id", subagent_run_id)
                    if isinstance(subagent_preset, str) and subagent_preset:
                        out.setdefault("preset", subagent_preset)
                    if isinstance(browser_agent_session, str) and browser_agent_session:
                        out.setdefault("browser_agent_session", browser_agent_session)
                    return out

                tool_call_id = getattr(fc, "call_id", None)
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    tool_call_id = new_tool_call_id()
                tool_execution_id = f"tool_{tool_call_id}"

                args = dict(kwargs)
                args_ref = artifact_store.put(
                    json.dumps(args, ensure_ascii=False, sort_keys=True, indent=2),
                    kind="tool_args",
                    meta={"summary": f"{bound_tool_name} args"},
                )
                planned = PlannedToolCall(
                    tool_execution_id=tool_execution_id,
                    tool_name=bound_tool_name,
                    tool_call_id=tool_call_id,
                    arguments=args,
                    arguments_ref=args_ref,
                )

                # Enforce Aura DENY decisions before executing the tool.
                inspection = tool_runtime.inspect(planned)
                if inspection.decision is InspectionDecision.DENY:
                    remaining_tool_calls = _consume_tool_call_budget()
                    error_code = inspection.error_code.value if inspection.error_code is not None else "permission"
                    status = _normalize_tool_end_status("blocked")
                    error_message = inspection.reason or inspection.action_summary or f"Tool call denied: {planned.tool_name}"
                    output_ref = artifact_store.put(
                        json.dumps(
                            {
                                "ok": False,
                                "tool": planned.tool_name,
                                "error_code": error_code,
                                "error": error_message,
                                "remaining_tool_calls": remaining_tool_calls,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                        kind="tool_output",
                        meta={"summary": f"{planned.tool_name} output ({status})"},
                    )
                    tool_message = json.dumps(
                        {
                            "ok": False,
                            "tool": planned.tool_name,
                            "output_ref": output_ref.to_dict(),
                            "error_code": error_code,
                            "error": error_message,
                            "result": None,
                            "remaining_tool_calls": remaining_tool_calls,
                        },
                        ensure_ascii=False,
                    )
                    tool_message_ref = artifact_store.put(
                        tool_message,
                        kind="tool_message",
                        meta={"summary": f"{planned.tool_name} tool_result ({status})"},
                    )
                    payload = {
                        "tool_execution_id": planned.tool_execution_id,
                        "tool_name": planned.tool_name,
                        "tool_call_id": planned.tool_call_id,
                        "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                        "status": status,
                        "status_legacy": "denied",
                        "duration_ms": 0,
                        "output_ref": output_ref.to_dict(),
                        "tool_message_ref": tool_message_ref.to_dict(),
                        "remaining_tool_calls": remaining_tool_calls,
                        "error_code": error_code,
                        "error": error_message,
                    }
                    emit(
                        kind=EventKind.TOOL_CALL_END,
                        payload=_with_subagent_meta(payload),
                    )
                    append_history(
                        CanonicalMessage(
                            role=CanonicalMessageRole.TOOL,
                            content=tool_message,
                            tool_call_id=planned.tool_call_id,
                            tool_name=planned.tool_name,
                        )
                    )
                    return tool_message

                emit(
                    kind=EventKind.TOOL_CALL_START,
                    payload=_with_subagent_meta(
                        {
                        "tool_execution_id": planned.tool_execution_id,
                        "tool_name": planned.tool_name,
                        "tool_call_id": planned.tool_call_id,
                        "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                        "arguments_ref": planned.arguments_ref.to_dict(),
                        }
                    ),
                )

                ctx = ToolExecutionContext(
                    session_id=str(getattr(run_context, "session_id", "") or ""),
                    request_id=meta.get("aura_request_id"),
                    turn_id=meta.get("aura_turn_id"),
                    tool_execution_id=planned.tool_execution_id,
                    event_bus=event_bus,
                    metadata=(dict(meta) if isinstance(meta, dict) else None),
                )

                tool = tool_runtime.get_tool(bound_tool_name)
                if tool is None:
                    remaining_tool_calls = _consume_tool_call_budget()
                    error_message = f"Unknown tool: {bound_tool_name}"
                    output_ref = artifact_store.put(
                        json.dumps(
                            {
                                "ok": False,
                                "tool": bound_tool_name,
                                "error_code": "tool_unknown",
                                "error": error_message,
                                "remaining_tool_calls": remaining_tool_calls,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                        kind="tool_output",
                        meta={"summary": f"{bound_tool_name} output (error)"},
                    )
                    tool_message = json.dumps(
                        {
                            "ok": False,
                            "tool": bound_tool_name,
                            "output_ref": output_ref.to_dict(),
                            "error_code": "tool_unknown",
                            "error": error_message,
                            "result": None,
                            "remaining_tool_calls": remaining_tool_calls,
                        },
                        ensure_ascii=False,
                    )
                    tool_message_ref = artifact_store.put(
                        tool_message,
                        kind="tool_message",
                        meta={"summary": f"{bound_tool_name} tool_result (error)"},
                    )
                    emit(
                        kind=EventKind.TOOL_CALL_END,
                        payload=_with_subagent_meta(
                            {
                            "tool_execution_id": planned.tool_execution_id,
                            "tool_name": planned.tool_name,
                            "tool_call_id": planned.tool_call_id,
                            "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                            "status": "failed",
                            "duration_ms": 0,
                            "output_ref": output_ref.to_dict(),
                            "tool_message_ref": tool_message_ref.to_dict(),
                            "details": None,
                            "remaining_tool_calls": remaining_tool_calls,
                            "error_code": "tool_unknown",
                            "error": error_message,
                            }
                        ),
                    )
                    append_history(
                        CanonicalMessage(
                            role=CanonicalMessageRole.TOOL,
                            content=tool_message,
                            tool_call_id=planned.tool_call_id,
                            tool_name=planned.tool_name,
                        )
                    )
                    return tool_message

                started = time.monotonic()
                try:
                    try:
                        from inspect import Parameter, signature

                        params = signature(tool.execute).parameters
                        accepts_context = "context" in params or any(p.kind is Parameter.VAR_KEYWORD for p in params.values())
                    except Exception:
                        accepts_context = False

                    if accepts_context:
                        raw = tool.execute(args=planned.arguments, project_root=tool_runtime.project_root, context=ctx)
                    else:
                        raw = tool.execute(args=planned.arguments, project_root=tool_runtime.project_root)
                except Exception as e:
                    remaining_tool_calls = _consume_tool_call_budget()
                    duration_ms = int((time.monotonic() - started) * 1000)
                    code = _classify_tool_exception(e)
                    output_ref = artifact_store.put(
                        json.dumps(
                            {
                                "ok": False,
                                "tool": planned.tool_name,
                                "error_code": code.value,
                                "error": str(e),
                                "remaining_tool_calls": remaining_tool_calls,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        ),
                        kind="tool_output",
                        meta={"summary": f"{planned.tool_name} output (error)"},
                    )
                    tool_message = json.dumps(
                        {
                            "ok": False,
                            "tool": planned.tool_name,
                            "output_ref": output_ref.to_dict(),
                            "error_code": code.value,
                            "error": str(e),
                            "result": None,
                            "remaining_tool_calls": remaining_tool_calls,
                        },
                        ensure_ascii=False,
                    )
                    tool_message_ref = artifact_store.put(
                        tool_message,
                        kind="tool_message",
                        meta={"summary": f"{planned.tool_name} tool_result (error)"},
                    )
                    emit(
                        kind=EventKind.TOOL_CALL_END,
                        payload=_with_subagent_meta(
                            {
                            "tool_execution_id": planned.tool_execution_id,
                            "tool_name": planned.tool_name,
                            "tool_call_id": planned.tool_call_id,
                            "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                            "status": "failed",
                            "duration_ms": duration_ms,
                            "output_ref": output_ref.to_dict(),
                            "tool_message_ref": tool_message_ref.to_dict(),
                            "details": None,
                            "remaining_tool_calls": remaining_tool_calls,
                            "error_code": code.value,
                            "error": str(e),
                            }
                        ),
                    )
                    append_history(
                        CanonicalMessage(
                            role=CanonicalMessageRole.TOOL,
                            content=tool_message,
                            tool_call_id=planned.tool_call_id,
                            tool_name=planned.tool_name,
                        )
                    )
                    return tool_message

                duration_ms = int((time.monotonic() - started) * 1000)
                remaining_tool_calls = _consume_tool_call_budget()
                output_ref = artifact_store.put(
                    json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2),
                    kind="tool_output",
                    meta={"summary": f"{planned.tool_name} output"},
                )
                status, ok, error_code, error = _classify_tool_result(tool_name=planned.tool_name, raw=raw)
                tool_message_payload: dict[str, Any] = {
                    "ok": ok,
                    "tool": planned.tool_name,
                    "output_ref": output_ref.to_dict(),
                    "result": raw,
                    "remaining_tool_calls": remaining_tool_calls,
                }
                if isinstance(error_code, str) and error_code.strip():
                    tool_message_payload["error_code"] = error_code
                if isinstance(error, str) and error.strip():
                    tool_message_payload["error"] = error
                tool_message = json.dumps(tool_message_payload, ensure_ascii=False)
                tool_message_ref = artifact_store.put(
                    tool_message,
                    kind="tool_message",
                    meta={"summary": f"{planned.tool_name} tool_result ({status})"},
                )

                emit(
                    kind=EventKind.TOOL_CALL_END,
                    payload=_with_subagent_meta(
                        {
                        "tool_execution_id": planned.tool_execution_id,
                        "tool_name": planned.tool_name,
                        "tool_call_id": planned.tool_call_id,
                        "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                        "status": status,
                        "duration_ms": duration_ms,
                        "output_ref": output_ref.to_dict(),
                        "tool_message_ref": tool_message_ref.to_dict(),
                        "details": None,
                        "remaining_tool_calls": remaining_tool_calls,
                        "error_code": error_code if status != "succeeded" else None,
                        "error": error if status != "succeeded" else None,
                        }
                    ),
                )

                append_history(
                    CanonicalMessage(
                        role=CanonicalMessageRole.TOOL,
                        content=tool_message,
                        tool_call_id=planned.tool_call_id,
                        tool_name=planned.tool_name,
                    )
                )
                return tool_message

            return _entrypoint

        functions.append(
            AgnoFunction(
                name=spec.name,
                description=spec.description,
                parameters=spec.input_schema,
                entrypoint=_make_entrypoint(spec.name),
                skip_entrypoint_processing=True,
                requires_confirmation=_should_require_confirmation(tool_name=spec.name, approval_mode=approval_mode),
            )
        )

    return AgnoToolset(functions=functions)
