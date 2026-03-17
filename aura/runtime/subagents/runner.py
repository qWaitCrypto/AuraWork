from __future__ import annotations

import fnmatch
import json
import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..ids import new_id, new_tool_call_id, now_ts_ms
from ..llm.errors import ModelResolutionError
from ..agent_browser import agent_browser_session_for_subagent_run
from ..error_codes import ErrorCode
from ..llm.router import ModelRouter
from ..llm.types import ModelRequirements, ModelRole, ToolSpec
from ..llm.tool_schema_compat import adapt_tool_specs_for_profile
from ..models import ApprovalRequest, SubagentArtifact, SubagentReceipt as ResultReceipt, SubagentResult, WorkSpec
from ..orchestrator_helpers import _summarize_text, _summarize_tool_for_ui
from ..protocol import Event, EventKind, EVENT_SCHEMA_VERSION
from ..prompts.template import render_prompt_template
from ..stores import ArtifactStore
from ..tools.runtime import InspectionDecision, InspectionResult, ToolExecutionContext, ToolRuntime
from .approver import maybe_apply_approval_agent
from .presets import SubagentPreset


logger = logging.getLogger(__name__)


def _derive_run_paths(*, work_spec: WorkSpec | None, fallback_run_id: str) -> tuple[str, str]:
    """
    Derive per-run artifacts locations.

    Prefer a WorkSpec-derived directory under `artifacts/subagent_runs/...` so that:
    - WorkSpec expectations align with where the subagent writes intermediate files (e.g. plan.json)
    - Approval decisions can be made consistently
    """

    run_dir = f"artifacts/subagent_runs/{fallback_run_id}"
    if work_spec is not None:
        candidates: list[str] = []
        for out in work_spec.expected_outputs or []:
            p = out.path
            if not isinstance(p, str):
                continue
            rel = p.strip().lstrip("./")
            if not rel:
                continue

            # Accept either a directory (`.../`) or a file path.
            candidate = rel.rstrip("/") if rel.endswith("/") else str(Path(rel).parent)
            if candidate in ("", "."):
                continue
            if candidate.startswith("artifacts/") and candidate != "artifacts":
                candidates.append(candidate)
                continue

        if candidates:
            # Prefer the most specific artifacts subdir (deepest path).
            run_dir = sorted(candidates, key=lambda s: (-len(Path(s).parts), s))[0]

    return run_dir, f"{run_dir}/plan.json"


_SHELL_METACHARS = {"&&", "||", ";", "|", "&"}


def _is_rel_path_within_any_root(*, project_root: Path, rel: str, roots: list[str]) -> bool:
    rel_path = Path(rel)
    if rel_path.is_absolute():
        return False
    project_root = project_root.resolve()
    candidate = (project_root / rel_path).resolve()
    if candidate != project_root and project_root not in candidate.parents:
        return False
    for root in roots:
        root_path = Path(root)
        if root_path.is_absolute():
            return False
        resolved_root = (project_root / root_path).resolve()
        if candidate == resolved_root or resolved_root in candidate.parents:
            return True
    return False


def _mkdirp_rel_dir(*, project_root: Path, rel_dir: str) -> None:
    """
    Best-effort create a workspace-relative directory.

    This avoids brittle failures in subagents (notably browser_worker) that set `cwd` to a WorkSpec
    workspace root like `artifacts/subagent_runs/<node_id>` which may not exist yet.
    """

    if not isinstance(rel_dir, str):
        return
    rel = rel_dir.strip().lstrip("./").rstrip("/")
    if not rel or rel == ".":
        return
    rel_path = Path(rel)
    if rel_path.is_absolute():
        return
    project_root = project_root.resolve()
    try:
        resolved = (project_root / rel_path).resolve()
    except Exception:
        return
    if resolved != project_root and project_root not in resolved.parents:
        return
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except Exception:
        return


def _ensure_work_spec_dirs_exist(
    *,
    project_root: Path,
    work_spec: WorkSpec | None,
    run_artifacts_dir: str,
    plan_json_path: str,
) -> None:
    if work_spec is None:
        return

    # Create declared workspace roots so tools can safely use them as `cwd`.
    for root in work_spec.resource_scope.workspace_roots or []:
        _mkdirp_rel_dir(project_root=project_root, rel_dir=str(root))

    # Also ensure the runner's plan/artifacts dirs exist.
    _mkdirp_rel_dir(project_root=project_root, rel_dir=run_artifacts_dir)
    _mkdirp_rel_dir(project_root=project_root, rel_dir=str(Path(plan_json_path).parent))

    # And parents for any explicit expected output paths.
    for out in work_spec.expected_outputs or []:
        p = out.path
        if not isinstance(p, str):
            continue
        rel = p.strip().lstrip("./")
        if not rel or rel.endswith("/"):
            _mkdirp_rel_dir(project_root=project_root, rel_dir=rel)
            continue
        _mkdirp_rel_dir(project_root=project_root, rel_dir=str(Path(rel).parent))


def _is_safe_skill_runner_shell_command(
    *,
    command: str,
    allowed_runner_scripts: list[str],
    project_root: Path,
    work_spec: WorkSpec | None,
) -> bool:
    """
    Decide whether a `shell__run` command is safe enough to auto-approve for a subagent preset.

    We only auto-approve the **skill runner** invocation (`python <skill_root>/scripts/run.py ...`)
    when:
    - it is a single command (no shell chaining),
    - the runner script matches the preset allowlist (project-relative),
    - the plan path + artifacts dir are within WorkSpec.workspace_roots (when provided).
    """

    if not isinstance(command, str) or not command.strip():
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    if any(tok in _SHELL_METACHARS for tok in parts):
        return False
    if parts[0] not in {"python", "python3"}:
        return False
    if len(parts) < 4:
        # python <run.py> <in_file> <plan.json> [--artifacts-dir ...]
        return False

    script = parts[1]
    script_path = Path(script)
    if script_path.is_absolute():
        return False
    script_norm = str(Path(script))
    allow_norm = {str(Path(p)) for p in allowed_runner_scripts}
    if script_norm not in allow_norm:
        return False
    try:
        resolved_script = (project_root / script_path).resolve()
    except Exception:
        return False
    project_root = project_root.resolve()
    if project_root not in resolved_script.parents:
        return False

    plan_json = parts[3]
    artifacts_dir: str | None = None
    out_path: str | None = None
    for i, tok in enumerate(parts):
        if tok == "--artifacts-dir" and i + 1 < len(parts):
            artifacts_dir = parts[i + 1]
            break
    for i, tok in enumerate(parts):
        if tok == "--out" and i + 1 < len(parts):
            out_path = parts[i + 1]
            break

    if work_spec is None:
        return True

    roots = list(work_spec.resource_scope.workspace_roots or [])
    if not roots:
        return False
    if not _is_rel_path_within_any_root(project_root=project_root, rel=plan_json, roots=roots):
        return False
    if artifacts_dir is None:
        return False
    if not _is_rel_path_within_any_root(project_root=project_root, rel=artifacts_dir, roots=roots):
        return False
    if out_path is not None and not _is_rel_path_within_any_root(project_root=project_root, rel=out_path, roots=roots):
        return False
    return True


@dataclass(frozen=True, slots=True)
class ToolCallReceipt:
    tool_execution_id: str | None
    tool_name: str
    tool_call_id: str
    status: str
    duration_ms: int | None
    summary: str | None
    output_ref: dict[str, Any] | None
    tool_message_ref: dict[str, Any] | None
    error_code: str | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_execution_id": self.tool_execution_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "summary": self.summary,
            "output_ref": self.output_ref,
            "tool_message_ref": self.tool_message_ref,
            "error_code": self.error_code,
            "error": self.error,
        }


def _tool_allowed(tool_name: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    for pat in patterns:
        if not isinstance(pat, str):
            continue
        pat = pat.strip()
        if not pat:
            continue
        if pat == tool_name:
            return True
        if fnmatch.fnmatch(tool_name, pat):
            return True
    return False


def _filter_tool_specs(*, tool_specs: list[ToolSpec], allowlist: list[str]) -> list[ToolSpec]:
    out: list[ToolSpec] = []
    for spec in tool_specs:
        if spec.name == "subagent__run":
            continue
        if _tool_allowed(spec.name, allowlist):
            out.append(spec)
    return out


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text


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


def _resolve_status_from_report(
    *,
    current_status: str,
    report: Any,
    needs_approval: dict[str, Any] | None,
) -> tuple[str, str | None]:
    report_status = _status_from_report(report)
    if report_status is None or report_status == current_status:
        return current_status, None

    if report_status == "needs_approval" and needs_approval is None:
        return "failed", "Subagent report requested approval without a pending approval payload."

    if current_status in {"needs_approval", "needs_user_takeover"} and report_status == "completed":
        # Keep unresolved control-flow states authoritative.
        return current_status, None

    return report_status, None


def _report_requests_user_takeover(report: Any) -> bool:
    if not isinstance(report, dict):
        return False

    status_raw = str(report.get("status") or "").strip().lower()
    if status_raw in {"needs_user_takeover", "user_takeover_required", "needs_takeover"}:
        return True

    next_step = report.get("next_step_suggestion")
    if isinstance(next_step, dict):
        rec = str(next_step.get("recommended") or "").strip().lower()
        if rec in {"needs_user_takeover", "user_takeover_required", "needs_takeover"}:
            return True
        action = str(next_step.get("action") or "").strip().lower()
        if action in {"needs_user_takeover", "user_takeover_required", "needs_takeover"}:
            return True

    return False


def _extract_proposals_from_report(report: Any) -> list[dict[str, Any]]:
    if not isinstance(report, dict):
        return []
    proposals = report.get("proposals")
    if not isinstance(proposals, list):
        return []
    return [dict(p) for p in proposals if isinstance(p, dict)]


def _extract_artifacts_from_report(report: Any) -> list[SubagentArtifact]:
    if not isinstance(report, dict):
        return []
    raw = report.get("artifacts")
    if not isinstance(raw, list):
        return []
    out: list[SubagentArtifact] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        art_type = str(item.get("type") or "").strip()
        path = str(item.get("path") or "").strip()
        if not art_type or not path:
            continue
        fmt_raw = item.get("format")
        fmt = str(fmt_raw).strip() if isinstance(fmt_raw, str) and fmt_raw.strip() else None
        out.append(SubagentArtifact(type=art_type, path=path, format=fmt))
    return out


def _takeover_approval_request_from_report(report: Any, *, fallback_error: str | None = None) -> ApprovalRequest:
    next_step = report.get("next_step_suggestion") if isinstance(report, dict) and isinstance(report.get("next_step_suggestion"), dict) else {}
    action_summary = str(
        (report.get("action_summary") if isinstance(report, dict) else None)
        or next_step.get("action_summary")
        or "Please complete browser verification/login, then approve to resume."
    ).strip()
    reason = str(
        (report.get("reason") if isinstance(report, dict) else None)
        or next_step.get("reason")
        or next_step.get("message")
        or fallback_error
        or "Browser task requires user takeover (CAPTCHA/login/2FA)."
    ).strip()
    return ApprovalRequest(
        kind="user_takeover",
        action_summary=action_summary,
        risk_level="medium",
        reason=reason,
        tool_name=None,
    )


def _work_spec_public_payload(work_spec: WorkSpec | None) -> dict[str, Any] | None:
    if work_spec is None:
        return None
    try:
        payload = work_spec.to_public_dict()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _context_to_text(extra_context: Any) -> str:
    if not isinstance(extra_context, dict):
        return ""
    parts: list[str] = []

    text = extra_context.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(text.strip())

    files = extra_context.get("files")
    if isinstance(files, list) and files:
        lines: list[str] = ["File hints:"]
        for item in files:
            if isinstance(item, str) and item.strip():
                lines.append(f"- {item.strip()}")
                continue
            if isinstance(item, dict):
                path = item.get("path")
                max_chars = item.get("max_chars")
                if isinstance(path, str) and path.strip():
                    suffix = ""
                    if isinstance(max_chars, int) and not isinstance(max_chars, bool) and max_chars > 0:
                        suffix = f" (max_chars={max_chars})"
                    lines.append(f"- {path.strip()}{suffix}")
        if len(lines) > 1:
            parts.append("\n".join(lines))

    return "\n\n".join(parts).strip()


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


def _update_system_message_in_run(out: Any, *, system_message: str) -> None:
    messages = getattr(out, "messages", None)
    if not isinstance(messages, list):
        return
    for m in messages:
        if getattr(m, "role", None) == "system":
            try:
                m.content = system_message
            except Exception:
                logger.warning("Failed to update system message content in subagent run object.", exc_info=True)
            return


def _run_output_exceeded_tool_call_limit(out: Any) -> bool:
    messages = getattr(out, "messages", None)
    if not isinstance(messages, list):
        return False
    for m in messages:
        if getattr(m, "role", None) != "tool":
            continue
        content = getattr(m, "content", None)
        if isinstance(content, str) and content.startswith("Tool call limit reached."):
            return True
    return False


def run_subagent(
    *,
    preset: SubagentPreset,
    task: str,
    extra_context: Any,
    work_spec: WorkSpec | None = None,
    tool_allowlist: list[str],
    max_turns: int,
    max_tool_calls: int,
    model_router: ModelRouter,
    tool_registry: Any,
    tool_runtime: ToolRuntime,
    artifact_store: ArtifactStore,
    project_root: Path,
    exec_context: ToolExecutionContext | None,
) -> dict[str, Any]:
    """
    Run an isolated delegated task using agno Agent (no Team).

    This runner:
    - Restricts tools via a per-run allowlist (glob patterns).
    - Uses Aura ToolRuntime inspection for deny/approval decisions.
    - Supports in-place approval waits when an ApprovalManager is available in context.
    """

    project_root = Path(project_root).expanduser().resolve()
    subagent_run_id = new_id("subag")

    session_id = exec_context.session_id if exec_context is not None else ""
    request_id = exec_context.request_id if exec_context is not None else None
    turn_id = exec_context.turn_id if exec_context is not None else None
    event_bus = exec_context.event_bus if exec_context is not None else None
    approval_manager = exec_context.approval_manager if exec_context is not None else None
    event_loop = exec_context.event_loop if exec_context is not None else None
    browser_agent_session = (
        agent_browser_session_for_subagent_run(aura_session_id=session_id, subagent_run_id=subagent_run_id)
        if isinstance(session_id, str) and session_id.strip()
        else None
    )

    receipts: list[ToolCallReceipt] = []
    executed_tool_calls = 0
    needs_approval: dict[str, Any] | None = None
    work_spec_payload = _work_spec_public_payload(work_spec)

    def _emit_event(*, kind: EventKind, payload: dict[str, Any]) -> None:
        nonlocal executed_tool_calls
        payload = dict(payload)
        payload.setdefault("subagent_run_id", subagent_run_id)
        payload.setdefault("preset", preset.name)

        if kind is EventKind.TOOL_CALL_END:
            executed_tool_calls += 1
            tool_name = payload.get("tool_name")
            tool_call_id = payload.get("tool_call_id")
            if isinstance(tool_name, str) and tool_name and isinstance(tool_call_id, str) and tool_call_id:
                receipts.append(
                    ToolCallReceipt(
                        tool_execution_id=payload.get("tool_execution_id") if isinstance(payload.get("tool_execution_id"), str) else None,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        status=str(payload.get("status") or "unknown"),
                        duration_ms=int(payload["duration_ms"]) if isinstance(payload.get("duration_ms"), int) else None,
                        summary=payload.get("summary") if isinstance(payload.get("summary"), str) else None,
                        output_ref=payload.get("output_ref") if isinstance(payload.get("output_ref"), dict) else None,
                        tool_message_ref=payload.get("tool_message_ref") if isinstance(payload.get("tool_message_ref"), dict) else None,
                        error_code=payload.get("error_code") if isinstance(payload.get("error_code"), str) else None,
                        error=payload.get("error") if isinstance(payload.get("error"), str) else None,
                    )
                )

        if event_bus is None:
            return
        step_id = payload.get("tool_execution_id")
        payload_out = dict(payload or {})
        payload_out.setdefault("source", "subagent")
        if work_spec_payload is not None:
            payload_out.setdefault("work_spec", work_spec_payload)
        event = Event(
            kind=kind.value,
            payload=payload_out,
            session_id=session_id,
            event_id=new_id("evt"),
            timestamp=now_ts_ms(),
            sequence=None,
            request_id=request_id,
            turn_id=turn_id,
            step_id=step_id if isinstance(step_id, str) else None,
            schema_version=EVENT_SCHEMA_VERSION,
        )
        event_bus.publish(event)

    # Resolve model profile for the subagent.
    requirements = ModelRequirements(needs_tools=True)
    selected_role = ModelRole.SUBAGENT
    selected_profile_id: str | None = None
    fallback_used = False
    try:
        resolved = model_router.resolve(role=ModelRole.SUBAGENT, requirements=requirements)
        selected_profile_id = getattr(resolved.profile, "profile_id", None)
    except ModelResolutionError:
        fallback_used = True
        selected_role = ModelRole.MAIN
        resolved = model_router.resolve(role=ModelRole.MAIN, requirements=requirements)
        selected_profile_id = getattr(resolved.profile, "profile_id", None)

    # Build agno model (Aura-backed).
    try:
        from ..llm.agno_aura_model import build_aura_agno_model
    except Exception as e:  # pragma: no cover - optional dependency
        raise RuntimeError(f"agno model adapter unavailable: {e}") from e

    agno_model = build_aura_agno_model(profile=resolved.profile, project_root=project_root, session_id=session_id)

    # Build toolset for the delegated run.
    all_specs = tool_registry.list_specs() if hasattr(tool_registry, "list_specs") else []
    filtered_specs = _filter_tool_specs(tool_specs=list(all_specs), allowlist=list(tool_allowlist))
    filtered_specs = adapt_tool_specs_for_profile(tools=filtered_specs, profile=resolved.profile)

    from ..agno_tools import build_agno_toolset

    # Keep subagent tool messages isolated from the parent Aura chat history.
    subagent_tool_messages: list[Any] = []

    toolset = build_agno_toolset(
        tool_specs=filtered_specs,
        tool_runtime=tool_runtime,
        emit=_emit_event,
        append_history=subagent_tool_messages.append,
        event_bus=event_bus,
        tool_call_budget=max_tool_calls,
    )

    # Construct system and user messages.
    allowlisted_names = [s.name for s in filtered_specs]
    allowlist_block = "\n".join(f"- {n}" for n in allowlisted_names) if allowlisted_names else "- (none)"
    system_message = preset.load_prompt().rstrip() + (
        "\n\nAllowed tools (enforced by runner):\n" + allowlist_block + "\n"
    )
    system_message = system_message.rstrip() + (
        "\n\nTool call budget:\n"
        f"- max_tool_calls: {int(max_tool_calls)}\n"
        "- After each tool call, tool results include `remaining_tool_calls`.\n"
        "- When remaining_tool_calls is low, stop exploring and produce the final deliverable.\n"
    )
    run_artifacts_dir, plan_json_path = _derive_run_paths(work_spec=work_spec, fallback_run_id=subagent_run_id)
    _ensure_work_spec_dirs_exist(
        project_root=project_root,
        work_spec=work_spec,
        run_artifacts_dir=run_artifacts_dir,
        plan_json_path=plan_json_path,
    )
    system_message = render_prompt_template(
        system_message,
        vars={
            "SUBAGENT_RUN_ID": subagent_run_id,
            "RUN_ARTIFACTS_DIR": run_artifacts_dir,
            "PLAN_JSON_PATH": plan_json_path,
        },
    )
    if work_spec is not None:
        ws_lines: list[str] = ["WorkSpec (enforced):", "- goal:"]
        goal_lines = (work_spec.goal or "").strip().splitlines()
        if goal_lines:
            ws_lines.extend([f"  {ln}".rstrip() for ln in goal_lines])
        else:
            ws_lines.append("  (empty)")

        intent_items = work_spec.intent_items or []
        if intent_items:
            ws_lines.append("- intent_items:")
            for it in intent_items:
                ws_lines.append(f"  - {it.id}: {it.text}")

        inputs = work_spec.inputs or []
        if inputs:
            ws_lines.append("- inputs:")
            for inp in inputs:
                bits: list[str] = []
                if inp.type is not None:
                    bits.append(inp.type.value)
                if isinstance(inp.path, str) and inp.path.strip():
                    bits.append(inp.path.strip())
                line = " ".join(bits).strip() or "(unspecified)"
                if isinstance(inp.description, str) and inp.description.strip():
                    line += f" — {inp.description.strip()}"
                ws_lines.append(f"  - {line}")

        if work_spec.constraints is not None:
            c = work_spec.constraints
            ws_lines.append("- constraints:")
            if isinstance(c.style, str) and c.style.strip():
                ws_lines.append(f"  - style: {c.style.strip()}")
            if isinstance(c.template, str) and c.template.strip():
                ws_lines.append(f"  - template: {c.template.strip()}")
            if c.deadline is not None:
                ws_lines.append(f"  - deadline: {c.deadline.isoformat()}")
            if isinstance(c.forbidden, list) and c.forbidden:
                ws_lines.append("  - forbidden:")
                for item in c.forbidden:
                    if isinstance(item, str) and item.strip():
                        ws_lines.append(f"    - {item.strip()}")

        roots = work_spec.resource_scope.workspace_roots or []
        if roots:
            ws_lines.append("- workspace_roots:")
            ws_lines.extend([f"  - {r}" for r in roots])
        domains = work_spec.resource_scope.domain_allowlist or []
        if domains:
            ws_lines.append("- domain_allowlist:")
            ws_lines.extend([f"  - {d}" for d in domains])
        ftypes = work_spec.resource_scope.file_type_allowlist or []
        if ftypes:
            ws_lines.append("- file_type_allowlist:")
            ws_lines.extend([f"  - {t}" for t in ftypes])
        outputs = work_spec.expected_outputs or []
        if outputs:
            ws_lines.append("- expected_outputs:")
            for o in outputs:
                ws_lines.append(f"  - {o.type.value}:{o.format} {o.path or ''}".rstrip())
        system_message = system_message.rstrip() + "\n\n" + "\n".join(ws_lines).rstrip() + "\n"
    context_text = _context_to_text(extra_context)
    user_text = task.strip()
    if context_text:
        user_text = user_text + "\n\nContext:\n" + context_text

    try:
        from agno.agent.agent import Agent as AgnoAgent
        from agno.models.message import Message as AgnoMessage
    except Exception as e:  # pragma: no cover - optional dependency
        raise RuntimeError(f"Agno is not available in this environment: {e}") from e

    agent = AgnoAgent(
        name=f"AuraSubagent:{preset.name}",
        model=agno_model,
        system_message=system_message,
        db=None,
        tools=toolset.functions,
        tool_call_limit=max_tool_calls,
        stream=False,
        stream_events=False,
    )

    metadata = {
        "aura_request_id": request_id,
        "aura_turn_id": turn_id,
        "aura_subagent_run_id": subagent_run_id,
        "aura_subagent_preset": preset.name,
        "aura_browser_agent_session": browser_agent_session,
    }
    input_messages = [AgnoMessage(role="user", content=user_text)]

    with tool_runtime.work_spec_context(work_spec):
        try:
            out = agent.run(
                input_messages,
                stream=False,
                session_id=session_id,
                metadata=metadata,
                add_history_to_context=False,
            )
        except KeyboardInterrupt:
            out = None
            assistant_text = ""
            status = "failed"
            report: Any = {"ok": False, "error": "cancelled"}
        except Exception as e:
            out = None
            assistant_text = ""
            status = "failed"
            report = {"ok": False, "error": str(e)}
        else:
            assistant_text = _extract_text(out)
            report = _json_or_text(assistant_text)
            status = _status_from_report(report) or (
                "needs_user_takeover" if _report_requests_user_takeover(report) else "completed"
            )

        # Handle agno confirmation pauses. Aura decides allow/deny/approval; subagent never creates approvals.
        pause_guard = 0
        while out is not None and getattr(out, "is_paused", False):
            pause_guard += 1
            if pause_guard > max(4, max_turns):
                status = "failed"
                report = {
                    "ok": False,
                    "error": "Exceeded pause/resume limit while processing tool confirmations.",
                    "error_code": "max_turns_exceeded",
                }
                break

            tools = getattr(out, "tools", None) or []
            paused = [t for t in tools if getattr(t, "requires_confirmation", False)]
            if not paused:
                status = "failed"
                report = {
                    "ok": False,
                    "error": "Run paused for unsupported requirement type (user_input/external_execution).",
                    "error_code": "unknown_request",
                }
                break
            tool = paused[0]
            tool_call_id = getattr(tool, "tool_call_id", None)
            tool_name = getattr(tool, "tool_name", None)
            tool_args = getattr(tool, "tool_args", None)
            if not isinstance(tool_call_id, str) or not tool_call_id:
                tool_call_id = new_tool_call_id()
                # Keep agno's tool execution coherent for resume.
                try:
                    tool.tool_call_id = tool_call_id
                except Exception:
                    logger.warning("Failed to backfill tool_call_id for paused subagent tool call.", exc_info=True)
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = "unknown"
                try:
                    tool.tool_name = tool_name
                except Exception:
                    logger.warning("Failed to backfill tool_name for paused subagent tool call.", exc_info=True)
            if not isinstance(tool_args, dict):
                tool_args = {}
                try:
                    tool.tool_args = tool_args
                except Exception:
                    logger.warning("Failed to backfill tool_args for paused subagent tool call.", exc_info=True)

            planned = tool_runtime.plan(
                tool_execution_id=f"tool_{tool_call_id}",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                arguments=dict(tool_args),
            )
            if not _tool_allowed(planned.tool_name, tool_allowlist):
                inspection = InspectionResult(
                    decision=InspectionDecision.DENY,
                    action_summary=f"Blocked tool outside subagent allowlist: {planned.tool_name}",
                    risk_level="high",
                    reason=f"Tool '{planned.tool_name}' is not permitted by this subagent's tool_allowlist.",
                    error_code=ErrorCode.PERMISSION,
                    diff_ref=None,
                )
            else:
                inspection = tool_runtime.inspect(planned)
            approver_trace: dict[str, Any] | None = None

            # Tier-2 approval: an LLM-only approver decides allow/deny/escalate using WorkSpec.
            if work_spec is not None and inspection.decision is InspectionDecision.REQUIRE_APPROVAL:
                _emit_event(
                    kind=EventKind.SUBAGENT_APPROVER_STARTED,
                    payload={
                        "tool_execution_id": planned.tool_execution_id,
                        "tool_call_id": planned.tool_call_id,
                        "tool_name": planned.tool_name,
                        "inspection": {
                            "decision": inspection.decision,
                            "risk_level": inspection.risk_level,
                            "action_summary": inspection.action_summary,
                            "reason": inspection.reason,
                            "error_code": (inspection.error_code.value if inspection.error_code is not None else "permission"),
                        },
                        "work_spec": {
                            "goal": work_spec.goal,
                            "expected_outputs": work_spec.to_public_dict().get("expected_outputs"),
                            "resource_scope": work_spec.to_public_dict().get("resource_scope"),
                        },
                    },
                )
                trace: dict[str, Any] = {}
                updated = maybe_apply_approval_agent(
                    agno_model=agno_model,
                    artifact_store=artifact_store,
                    preset=preset,
                    work_spec=work_spec,
                    planned=planned,
                    inspection=inspection,
                    trace=trace,
                )
                approver_trace = trace
                _emit_event(
                    kind=EventKind.SUBAGENT_APPROVER_COMPLETED,
                    payload={
                        "tool_execution_id": planned.tool_execution_id,
                        "tool_call_id": planned.tool_call_id,
                        "tool_name": planned.tool_name,
                        "inspection_before": {
                            "decision": inspection.decision,
                            "risk_level": inspection.risk_level,
                            "action_summary": inspection.action_summary,
                            "reason": inspection.reason,
                            "error_code": (inspection.error_code.value if inspection.error_code is not None else "permission"),
                        },
                        "inspection_after": {
                            "decision": updated.decision,
                            "risk_level": updated.risk_level,
                            "action_summary": updated.action_summary,
                            "reason": updated.reason,
                            "error_code": (
                                updated.error_code.value
                                if updated.error_code is not None
                                else (inspection.error_code.value if inspection.error_code is not None else None)
                            ),
                        },
                        "approver_trace": trace,
                    },
                )
                inspection = updated
            else:
                inspection = maybe_apply_approval_agent(
                    agno_model=agno_model,
                    artifact_store=artifact_store,
                    preset=preset,
                    work_spec=work_spec,
                    planned=planned,
                    inspection=inspection,
                )

            # Auto-approve allowlisted *skill runner* commands for subagents to avoid approval fatigue.
            if (
                inspection.decision is InspectionDecision.REQUIRE_APPROVAL
                and preset.safe_shell_prefixes
                and planned.tool_name == "shell__run"
            ):
                cmd = planned.arguments.get("command", "")
                if _is_safe_skill_runner_shell_command(
                    command=str(cmd),
                    allowed_runner_scripts=preset.safe_shell_prefixes,
                    project_root=project_root,
                    work_spec=work_spec,
                ):
                    inspection = type(inspection)(
                        decision=InspectionDecision.ALLOW,
                        action_summary=inspection.action_summary,
                        risk_level="low",
                        reason=f"Auto-approved safe skill runner for subagent preset: {preset.name}.",
                        error_code=inspection.error_code,
                        diff_ref=inspection.diff_ref,
                    )

            # Preset-specific auto-approval (testing / ergonomics). The main agent still enforces approvals.
            if (
                inspection.decision is InspectionDecision.REQUIRE_APPROVAL
                and planned.tool_name in set(preset.auto_approve_tools)
                and inspection.error_code is None
            ):
                inspection = type(inspection)(
                    decision=InspectionDecision.ALLOW,
                    action_summary=inspection.action_summary,
                    risk_level=inspection.risk_level,
                    reason=f"Auto-approved {planned.tool_name} for subagent preset: {preset.name} (no approval prompts).",
                    error_code=inspection.error_code,
                    diff_ref=inspection.diff_ref,
                )

            if inspection.decision is InspectionDecision.REQUIRE_APPROVAL:
                needs_approval = {
                    "tool_execution_id": planned.tool_execution_id,
                    "tool_name": planned.tool_name,
                    "tool_call_id": planned.tool_call_id,
                    "arguments_ref": planned.arguments_ref.to_dict(),
                    "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                    "action_summary": inspection.action_summary,
                    "risk_level": inspection.risk_level or "high",
                    "reason": inspection.reason,
                    "approver_trace": approver_trace,
                }
                if approval_manager is not None and event_loop is not None:
                    approval_id = f"tool-{subagent_run_id}-{tool_call_id}"
                    approval_request = {
                        "tool_name": planned.tool_name,
                        "action_summary": inspection.action_summary,
                        "risk_level": inspection.risk_level or "high",
                        "reason": inspection.reason,
                        "diff_ref": inspection.diff_ref.to_dict() if inspection.diff_ref is not None else None,
                    }
                    pending = approval_manager.register(approval_id, approval_request)
                    _emit_event(
                        kind=EventKind.TOOL_APPROVAL_REQUESTED,
                        payload={
                            "approval_id": approval_id,
                            "tool_execution_id": planned.tool_execution_id,
                            "tool_call_id": planned.tool_call_id,
                            "tool_name": planned.tool_name,
                            "action_summary": inspection.action_summary,
                            "risk_level": inspection.risk_level or "high",
                            "reason": inspection.reason,
                            "diff_ref": approval_request["diff_ref"],
                        },
                    )

                    got_decision = pending.event.wait(timeout=300)
                    approval_manager.unregister(approval_id)
                    decision = pending.decision[0] if got_decision else "deny"
                    _emit_event(
                        kind=EventKind.TOOL_APPROVAL_RESOLVED,
                        payload={
                            "approval_id": approval_id,
                            "tool_execution_id": planned.tool_execution_id,
                            "tool_call_id": planned.tool_call_id,
                            "tool_name": planned.tool_name,
                            "decision": decision,
                        },
                    )

                    allow = decision == "approve"
                    try:
                        tool.requires_confirmation = True
                    except Exception:
                        logger.warning("Failed to set requires_confirmation for resolved subagent approval.", exc_info=True)
                    try:
                        tool.confirmed = allow
                    except Exception:
                        logger.warning("Failed to set confirmed for resolved subagent approval.", exc_info=True)
                    try:
                        tool.confirmation_note = (
                            "User approved."
                            if allow
                            else "Denied or timed out."
                        )
                    except Exception:
                        logger.warning("Failed to set confirmation_note for resolved subagent approval.", exc_info=True)

                    # This approval was resolved in-place; do not leak stale pending flag.
                    needs_approval = None
                    _update_system_message_in_run(out, system_message=system_message)
                    out = agent.continue_run(
                        run_response=out,
                        stream=False,
                        session_id=session_id,
                        metadata=metadata,
                    )
                    assistant_text = _extract_text(out)
                    report = _json_or_text(assistant_text)
                    continue

                _emit_event(
                    kind=EventKind.TOOL_CALL_END,
                    payload={
                        "tool_execution_id": planned.tool_execution_id,
                        "tool_name": planned.tool_name,
                        "tool_call_id": planned.tool_call_id,
                        "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                        "status": "needs_approval",
                        "duration_ms": 0,
                        "output_ref": None,
                        "tool_message_ref": None,
                        "error_code": None,
                        "error": inspection.reason or inspection.action_summary,
                    },
                )
                status = "needs_approval"
                report = {
                    "ok": False,
                    "status": "needs_approval",
                    "needs_approval": [needs_approval],
                    "summary": "Subagent requested a tool that requires user approval.",
                }
                break

            if inspection.decision is InspectionDecision.ALLOW:
                try:
                    tool.requires_confirmation = True
                except Exception:
                    logger.warning("Failed to set requires_confirmation=True for approved subagent tool.", exc_info=True)
                try:
                    tool.confirmed = True
                except Exception:
                    logger.warning("Failed to set confirmed=True for approved subagent tool.", exc_info=True)
                try:
                    tool.confirmation_note = inspection.reason or inspection.action_summary
                except Exception:
                    logger.warning("Failed to set confirmation_note for approved subagent tool.", exc_info=True)
                _update_system_message_in_run(out, system_message=system_message)
                out = agent.continue_run(
                    run_response=out,
                    stream=False,
                    session_id=session_id,
                    metadata=metadata,
                )
                assistant_text = _extract_text(out)
                report = _json_or_text(assistant_text)
                continue

            # DENY
            _emit_event(
                kind=EventKind.TOOL_CALL_END,
                payload={
                    "tool_execution_id": planned.tool_execution_id,
                    "tool_name": planned.tool_name,
                    "tool_call_id": planned.tool_call_id,
                    "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                    "status": "blocked",
                    "status_legacy": "denied",
                    "duration_ms": 0,
                    "output_ref": None,
                    "tool_message_ref": None,
                    "error_code": (inspection.error_code.value if inspection.error_code is not None else "permission"),
                    "error": inspection.reason or inspection.action_summary,
                },
            )
            try:
                tool.requires_confirmation = True
            except Exception:
                logger.warning("Failed to set requires_confirmation=True for denied subagent tool.", exc_info=True)
            try:
                tool.confirmed = False
            except Exception:
                logger.warning("Failed to set confirmed=False for denied subagent tool.", exc_info=True)
            try:
                tool.confirmation_note = inspection.reason or inspection.action_summary
            except Exception:
                logger.warning("Failed to set confirmation_note for denied subagent tool.", exc_info=True)
            _update_system_message_in_run(out, system_message=system_message)
            out = agent.continue_run(
                run_response=out,
                stream=False,
                session_id=session_id,
                metadata=metadata,
            )
            assistant_text = _extract_text(out)
            report = _json_or_text(assistant_text)

    status, status_mismatch_error = _resolve_status_from_report(
        current_status=status,
        report=report,
        needs_approval=needs_approval,
    )
    if status_mismatch_error is not None:
        if isinstance(report, dict):
            report = dict(report)
            report.setdefault("status", "failed")
            report.setdefault("error", status_mismatch_error)
        else:
            report = {"status": "failed", "error": status_mismatch_error}

    if status == "completed" and _report_requests_user_takeover(report):
        status = "needs_user_takeover"

    error_code: str | None = "invalid_subagent_report" if status_mismatch_error is not None and status == "failed" else None
    if status == "needs_user_takeover":
        error_code = "approval_pending"
        if isinstance(report, dict):
            report = dict(report)
            report.setdefault("status", "needs_user_takeover")

    warnings: list[dict[str, str]] = []
    if status == "completed" and out is not None and _run_output_exceeded_tool_call_limit(out):
        warnings.append(
            {
                "code": "max_tool_calls_exceeded",
                "message": (
                    "Tool call limit reached. Further tool calls were blocked; output may be partial. "
                    "Returning the best-effort final result."
                ),
            }
        )

    # Best-effort turn counting (agno does not expose a direct max_turns guard).
    if status == "completed" and out is not None:
        messages = getattr(out, "messages", None) or []
        turns = len([m for m in messages if getattr(m, "role", None) == "assistant"])
        if isinstance(turns, int) and turns > max_turns:
            status = "failed"
            error_code = "max_turns_exceeded"
            report = {
                "ok": False,
                "status": "failed",
                "error_code": error_code,
                "summary": "Subagent exceeded max_turns.",
            }

    transcript = {
        "subagent_run_id": subagent_run_id,
        "browser_agent_session": browser_agent_session,
        "preset": preset.name,
        "status": status,
        "selected_role": selected_role.value,
        "selected_profile_id": selected_profile_id,
        "fallback_used": fallback_used,
        "tool_allowlist": list(tool_allowlist),
        "limits": {"max_turns": int(max_turns), "max_tool_calls": int(max_tool_calls)},
        "executed_tool_calls": executed_tool_calls,
        "receipts": [r.to_dict() for r in receipts],
        "report": report,
        "assistant_text": assistant_text,
        "needs_approval": [needs_approval] if needs_approval is not None else [],
        "error_code": error_code,
        "warnings": list(warnings),
    }
    transcript_ref = artifact_store.put(
        json.dumps(transcript, ensure_ascii=False, sort_keys=True, indent=2),
        kind="subagent_transcript",
        meta={"summary": f"Subagent transcript ({preset.name})", "text_summary": _summarize_text(assistant_text)},
    )

    typed_receipts = [
        ResultReceipt(
            tool=r.tool_name,
            args_summary=r.summary or "",
            result_summary=r.error or (r.status or ""),
        )
        for r in receipts
    ]
    typed_artifacts = _extract_artifacts_from_report(report)
    typed_proposals = _extract_proposals_from_report(report)

    approval_request_model: ApprovalRequest | None = None
    if status == "needs_approval" and isinstance(needs_approval, dict):
        approval_request_model = ApprovalRequest(
            kind="tool_approval",
            action_summary=str(needs_approval.get("action_summary") or "Subagent requested approval").strip(),
            risk_level=str(needs_approval.get("risk_level") or "high").strip() or "high",
            reason=str(needs_approval.get("reason") or "").strip(),
            tool_name=str(needs_approval.get("tool_name") or "").strip() or None,
        )
    elif status == "needs_user_takeover":
        approval_request_model = _takeover_approval_request_from_report(report, fallback_error="User takeover required.")

    error_text: str | None = None
    if status == "failed":
        if isinstance(report, dict):
            error_text = str(report.get("error") or report.get("summary") or report.get("message") or "").strip() or None
        elif isinstance(report, str):
            error_text = report.strip() or None

    typed_data: dict[str, Any] = {
        "ok": status == "completed",
        "subagent_run_id": subagent_run_id,
        "browser_agent_session": browser_agent_session,
        "preset": preset.name,
        "selected_role": selected_role.value,
        "selected_profile_id": selected_profile_id,
        "fallback_used": fallback_used,
        "tool_allowlist": list(tool_allowlist),
        "limits": {"max_turns": int(max_turns), "max_tool_calls": int(max_tool_calls)},
        "executed_tool_calls": executed_tool_calls,
        "report": report,
        "transcript_ref": transcript_ref.to_dict(),
        "warnings": list(warnings),
        "error_code": error_code,
    }
    if needs_approval is not None:
        typed_data["needs_approval"] = [dict(needs_approval)]

    typed_result = SubagentResult(
        status=status,
        report=report,
        receipts=typed_receipts,
        artifacts=typed_artifacts,
        proposals=typed_proposals,
        approval_request=approval_request_model,
        error=error_text,
        data=typed_data,
    ).model_dump()

    return {
        "ok": status == "completed",
        "subagent_run_id": subagent_run_id,
        "browser_agent_session": browser_agent_session,
        "preset": preset.name,
        "status": status,
        "needs_approval": [needs_approval] if needs_approval is not None else [],
        "error_code": error_code,
        "warnings": list(warnings),
        "selected_role": selected_role.value,
        "selected_profile_id": selected_profile_id,
        "fallback_used": fallback_used,
        "tool_allowlist": list(tool_allowlist),
        "limits": {"max_turns": int(max_turns), "max_tool_calls": int(max_tool_calls)},
        "executed_tool_calls": executed_tool_calls,
        "receipts": typed_result.get("receipts", []),
        "report": report,
        "transcript_ref": transcript_ref.to_dict(),
        **typed_result,
    }
