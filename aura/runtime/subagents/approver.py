from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..error_codes import ErrorCode
from ..models import WorkSpec
from ..stores import ArtifactStore
from ..subagents.presets import load_prompt_asset, SubagentPreset
from ..tools.runtime import InspectionDecision, InspectionResult, PlannedToolCall


@dataclass(frozen=True, slots=True)
class ApprovalAgentOutput:
    decision: str  # allow | require_user | deny
    reason: str
    safety_notes: list[str]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    s = (text or "").strip()
    if not s:
        return None
    decoder = json.JSONDecoder()
    # Find the first '{' and try raw_decode from there.
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(s[i:])
        except Exception:
            continue
        return obj if isinstance(obj, dict) else None
    return None


def _extract_text(out: Any) -> str:
    content = getattr(out, "content", None)
    if isinstance(content, str):
        return content
    messages = getattr(out, "messages", None)
    if isinstance(messages, list):
        for m in reversed(messages):
            if getattr(m, "role", None) == "assistant":
                if hasattr(m, "get_content_string"):
                    text = m.get_content_string()
                    if isinstance(text, str):
                        return text
                raw = getattr(m, "content", None)
                if isinstance(raw, str):
                    return raw
    if content is None:
        return ""
    return str(content)


def _read_artifact_text(store: ArtifactStore, ref: Any, *, max_chars: int = 6000) -> str | None:
    if ref is None:
        return None
    try:
        raw = store.get(ref)
    except Exception:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def maybe_apply_approval_agent(
    *,
    agno_model: Any,
    artifact_store: ArtifactStore,
    preset: SubagentPreset,
    work_spec: WorkSpec | None,
    planned: PlannedToolCall,
    inspection: InspectionResult,
    trace: dict[str, Any] | None = None,
) -> InspectionResult:
    """
    Tier-2 approval: use an LLM-only approval agent to auto-approve or deny a tool call.

    Only runs when:
    - WorkSpec is provided (so the agent can evaluate scope/intents)
    - ToolRuntime returned REQUIRE_APPROVAL
    """

    if trace is not None:
        trace.setdefault("attempted", True)

    if work_spec is None:
        if trace is not None:
            trace.setdefault("skipped", "no_work_spec")
        return inspection
    if inspection.decision is not InspectionDecision.REQUIRE_APPROVAL:
        if trace is not None:
            trace.setdefault("skipped", "inspection_not_require_approval")
        return inspection

    try:
        from agno.agent.agent import Agent as AgnoAgent
        from agno.models.message import Message as AgnoMessage
    except Exception:
        # If agno is unavailable, fall back to the default require_approval behavior.
        if trace is not None:
            trace.setdefault("skipped", "agno_unavailable")
        return inspection

    system_message = load_prompt_asset("subagent_approver.md").rstrip() + "\n"

    diff_preview = _read_artifact_text(artifact_store, inspection.diff_ref) if inspection.diff_ref is not None else None

    payload: dict[str, Any] = {
        "work_spec": work_spec.to_public_dict(),
        "tool_call": {
            "tool_name": planned.tool_name,
            "arguments": planned.arguments,
            "action_summary": inspection.action_summary,
            "risk_level": inspection.risk_level,
            "reason": inspection.reason,
            "error_code": (inspection.error_code.value if inspection.error_code is not None else None),
        },
        "diff_preview": diff_preview,
        "preset_hints": {"preset_name": preset.name, "prefer_auto_approve": planned.tool_name in set(preset.auto_approve_tools)},
    }

    agent = AgnoAgent(
        name="AuraSubagent:approver",
        model=agno_model,
        system_message=system_message,
        db=None,
        tools=[],
        tool_call_limit=1,
        stream=False,
        stream_events=False,
    )

    user_msg = AgnoMessage(role="user", content=json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    try:
        out = agent.run([user_msg], stream=False, session_id="approver", metadata={}, add_history_to_context=False)
    except Exception as e:
        if trace is not None:
            trace.setdefault("error", f"run_failed: {type(e).__name__}: {e}")
        return inspection

    if getattr(out, "is_paused", False):
        if trace is not None:
            trace.setdefault("paused", True)
        return inspection

    raw_text = _extract_text(out)
    parsed = _extract_json_object(raw_text)
    if not isinstance(parsed, dict):
        if trace is not None:
            trace.setdefault("parsed", False)
        return inspection

    decision = str(parsed.get("decision") or "").strip().lower()
    reason = str(parsed.get("reason") or "").strip()
    safety_notes_raw = parsed.get("safety_notes")
    safety_notes: list[str] = []
    if isinstance(safety_notes_raw, list):
        for item in safety_notes_raw:
            if isinstance(item, str) and item.strip():
                safety_notes.append(item.strip())

    # Back-compat: allow prompts that only output `reasons` without a `reason` summary.
    if not reason:
        reasons_raw = parsed.get("reasons")
        if isinstance(reasons_raw, list):
            parts: list[str] = []
            for item in reasons_raw:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
            if parts:
                reason = " | ".join(parts[:4])

    if trace is not None:
        trace["parsed"] = True
        trace["output"] = {
            "decision": decision,
            "reason": reason,
            "safety_notes": safety_notes,
        }

    # Normalize decisions
    if decision == "allow":
        return type(inspection)(
            decision=InspectionDecision.ALLOW,
            action_summary=inspection.action_summary,
            risk_level=inspection.risk_level,
            reason=reason or "Auto-approved by approval agent.",
            error_code=inspection.error_code,
            diff_ref=inspection.diff_ref,
        )
    if decision == "deny":
        return type(inspection)(
            decision=InspectionDecision.DENY,
            action_summary=inspection.action_summary,
            risk_level=inspection.risk_level or "high",
            reason=reason or "Denied by approval agent.",
            error_code=inspection.error_code or ErrorCode.PERMISSION,
            diff_ref=inspection.diff_ref,
        )

    # "require_user" or unknown => keep REQUIRE_APPROVAL
    return inspection
