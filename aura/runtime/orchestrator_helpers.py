from __future__ import annotations

import json
from typing import Any

from .llm.types import (
    CanonicalMessage,
    CanonicalMessageRole,
    CanonicalRequest,
    ToolCall,
)
from .tools.runtime import PlannedToolCall


def _summarize_text(text: str, *, max_len: int = 160) -> str:
    s = " ".join(text.strip().split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _summarize_tool_for_ui(tool_name: str, arguments: dict[str, Any]) -> str:
    def _q(s: str) -> str:
        # Quote without backticks so terminals that don't render markdown still look OK.
        return f'"{s}"'

    if tool_name == "project__read_text":
        path = arguments.get("path")
        if isinstance(path, str) and path:
            return f"Read {_q(path)}"
        return "Read file"

    if tool_name == "project__search_text":
        query = arguments.get("query")
        path = arguments.get("path")
        if isinstance(query, str) and query:
            if isinstance(path, str) and path:
                return f"Search {_q(query)} in {_q(path)}"
            return f"Search {_q(query)}"
        return "Search text"

    if tool_name == "project__list_dir":
        path = arguments.get("path") or "."
        recursive = arguments.get("recursive")
        if isinstance(path, str) and path:
            suffix = " (recursive)" if recursive is True else ""
            return f"List {_q(path)}{suffix}"
        return "List directory"

    if tool_name == "project__glob":
        pats = arguments.get("patterns")
        base = arguments.get("base")
        if isinstance(pats, list) and pats:
            n = len(pats)
            if isinstance(base, str) and base:
                return f"Glob {n} pattern(s) in {_q(base)}"
            return f"Glob {n} pattern(s)"
        return "Glob files"

    if tool_name == "project__read_text_many":
        paths = arguments.get("paths")
        if isinstance(paths, list) and paths:
            return f"Read {len(paths)} file(s)"
        return "Read many files"

    if tool_name == "project__text_stats":
        path = arguments.get("path")
        if isinstance(path, str) and path:
            return f"Text stats {_q(path)}"
        return "Text stats"

    if tool_name == "project__apply_patch":
        return "Apply patch"

    if tool_name == "project__patch":
        return "Apply patch"

    if tool_name == "project__apply_edits":
        ops = arguments.get("ops")
        if isinstance(ops, list) and ops:
            return f"Apply edits ({len(ops)} op(s))"
        return "Apply edits"

    if tool_name == "shell__run":
        command = arguments.get("command")
        if isinstance(command, str) and command.strip():
            one_line = " ".join(command.strip().splitlines()).strip()
            if len(one_line) > 80:
                one_line = one_line[:79] + "…"
            return f"Run $ {one_line}"
        return "Run shell command"

    if tool_name == "subagent__run":
        preset = arguments.get("preset")
        task = arguments.get("task")
        task_snippet = _summarize_text(task, max_len=80) if isinstance(task, str) and task.strip() else None

        if isinstance(preset, str) and preset.strip():
            head = f"Subagent: {preset.strip()}"
        else:
            head = "Subagent run"
        if task_snippet:
            return f"{head} — {task_snippet}"
        return head

    if tool_name == "browser__run":
        steps = arguments.get("steps")
        if isinstance(steps, list) and steps:
            first = steps[0]
            if isinstance(first, str) and first.strip():
                head = first.strip()
            elif isinstance(first, list) and first and all(isinstance(x, str) for x in first):
                head = " ".join(x for x in first if x)
            elif isinstance(first, dict):
                cmd = first.get("command")
                head = str(cmd).strip() if isinstance(cmd, str) and cmd.strip() else "step"
            else:
                head = "step"
            suffix = ""
            if len(steps) > 1:
                suffix = f" (+{len(steps) - 1} step(s))"
            return f"Browser: {head}{suffix}"
        return "Browser automation"

    if tool_name == "session__search":
        query = arguments.get("query")
        if isinstance(query, str) and query:
            return f"Search sessions {_q(query)}"
        return "Search sessions"

    if tool_name == "session__export":
        sid = arguments.get("session_id")
        if isinstance(sid, str) and sid:
            return f"Export session ({_q(sid)})"
        return "Export session"

    if tool_name == "mcp__list_servers":
        return "List MCP servers"

    if tool_name == "mcp__list_tools":
        server = arguments.get("server")
        if isinstance(server, str) and server:
            tool_name_filter = arguments.get("tool_name")
            if isinstance(tool_name_filter, str) and tool_name_filter:
                return f"List MCP tools ({_q(server)}:{_q(tool_name_filter)})"
            return f"List MCP tools ({_q(server)})"
        return "List MCP tools"

    if tool_name == "mcp__call_tool":
        server = arguments.get("server")
        tool = arguments.get("tool")
        if isinstance(server, str) and isinstance(tool, str) and server and tool:
            return f"Call MCP ({_q(server)}:{_q(tool)})"
        return "Call MCP tool"

    if tool_name == "update_plan":
        return "Update plan"
    if tool_name == "update_todo":
        return "Update todo"

    if tool_name.startswith("skill__"):
        name = arguments.get("name")
        if isinstance(name, str) and name:
            return f"Skill {tool_name} ({_q(name)})"
        return f"Skill {tool_name}"

    if tool_name.startswith("spec__"):
        if tool_name == "spec__apply":
            proposal_id = arguments.get("proposal_id")
            if isinstance(proposal_id, str) and proposal_id:
                return f"Spec apply ({_q(proposal_id)})"
            return "Spec apply"
        if tool_name == "spec__seal":
            label = arguments.get("label")
            if isinstance(label, str) and label:
                return f"Spec seal ({_q(label)})"
            return "Spec seal"
        return f"Spec {tool_name}"

    return tool_name


def _canonical_request_to_redacted_dict(request: CanonicalRequest) -> dict[str, Any]:
    return {
        "system": request.system,
        "messages": [_canonical_message_to_redacted_dict(m) for m in request.messages],
        "params": dict(request.params),
        "tools": [t.__dict__ for t in request.tools],
    }


def _canonical_message_to_redacted_dict(msg: CanonicalMessage) -> dict[str, Any]:
    out: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    if msg.tool_name is not None:
        out["tool_name"] = msg.tool_name
    if msg.tool_calls:
        out["tool_calls"] = [
            {"tool_call_id": tc.tool_call_id, "name": tc.name, "arguments": tc.arguments} for tc in msg.tool_calls
        ]
    return out


def _planned_tool_call_descriptor(planned: PlannedToolCall) -> dict[str, Any]:
    return {
        "tool_execution_id": planned.tool_execution_id,
        "tool_name": planned.tool_name,
        "tool_call_id": planned.tool_call_id,
        "arguments_ref": planned.arguments_ref.to_dict(),
    }


def _planned_tool_call_from_descriptor(raw: dict[str, Any], *, read_artifact_text) -> PlannedToolCall:
    tool_execution_id = str(raw.get("tool_execution_id") or "")
    tool_name = str(raw.get("tool_name") or "")
    tool_call_id = str(raw.get("tool_call_id") or "")
    args_ref_raw = raw.get("arguments_ref")
    if not tool_execution_id or not tool_name or not tool_call_id or not isinstance(args_ref_raw, dict):
        raise ValueError("Missing required tool call fields.")
    args_json = read_artifact_text(args_ref_raw)
    args_any = json.loads(args_json)
    if not isinstance(args_any, dict):
        raise ValueError("Tool arguments artifact is not a JSON object.")
    from .protocol import ArtifactRef as _ArtifactRef

    return PlannedToolCall(
        tool_execution_id=tool_execution_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=args_any,
        arguments_ref=_ArtifactRef.from_dict(args_ref_raw),
    )


def _tool_calls_from_payload(raw: Any, *, read_artifact_text) -> list[ToolCall]:
    if not isinstance(raw, list):
        return []
    out: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool_call_id = item.get("tool_call_id")
        name = item.get("tool_name") or item.get("name")
        args_ref_raw = item.get("arguments_ref")
        thought_signature = item.get("thought_signature") or item.get("thoughtSignature")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(args_ref_raw, dict):
            continue
        try:
            args_json = read_artifact_text(args_ref_raw)
            args_any = json.loads(args_json)
        except Exception:
            continue
        if not isinstance(args_any, dict):
            continue
        sig = thought_signature.strip() if isinstance(thought_signature, str) and thought_signature.strip() else None
        out.append(ToolCall(tool_call_id=tool_call_id, name=name, arguments=args_any, raw_arguments=None, thought_signature=sig))
    return out


def _canonical_request_from_artifact_json(raw: str) -> CanonicalRequest:
    data = json.loads(raw)
    system = data.get("system")
    messages_raw = data.get("messages") or []
    if not isinstance(messages_raw, list):
        raise ValueError("messages must be a list")
    messages: list[CanonicalMessage] = []
    for item in messages_raw:
        if not isinstance(item, dict):
            continue
        role_raw = str(item.get("role") or "")
        try:
            role = CanonicalMessageRole(role_raw)
        except ValueError:
            continue
        messages.append(CanonicalMessage(role=role, content=str(item.get("content") or "")))
    params = data.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    return CanonicalRequest(system=str(system) if system is not None else None, messages=messages, params=params)
