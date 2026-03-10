from __future__ import annotations
import logging

import asyncio
import json
import os
import threading
import time
from copy import deepcopy
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from typing import Any

from .agent_surface import SpecStatusSummary, build_agent_surface
from .approval import ApprovalDecision, ApprovalRecord, ApprovalStatus
from .context_mgmt import (
    approx_tokens_from_json,
    canonical_request_to_dict,
    compute_context_left_percent,
    resolve_context_limit_tokens,
)
from .dag_plan_runner import DAGPlanRunner
from .error_codes import ErrorCode
from .event_bus import EventBus
from .ids import new_id, new_tool_call_id, now_ts_ms
from .llm.config import ModelConfig
from .llm.client_exec_anthropic import complete_anthropic, stream_anthropic
from .llm.client_exec_openai_codex import complete_openai_codex, stream_openai_codex
from .llm.client_exec_gemini import complete_gemini, stream_gemini
from .llm.client_exec_openai_compatible import complete_openai_compatible, stream_openai_compatible
from .llm.errors import CancellationToken, LLMRequestError, ModelResolutionError, wrap_provider_exception
from .llm.router import ModelRouter
from .llm.trace import LLMTrace
from .llm.types import (
    CanonicalMessage,
    CanonicalMessageRole,
    CanonicalRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMStreamEventKind,
    ModelRequirements,
    ModelRole,
    ModelProfile,
    ProviderKind,
    ToolCall,
    ToolSpec,
)
from .orchestrator_helpers import (
    _canonical_request_to_redacted_dict,
    _summarize_text,
    _summarize_tool_for_ui,
    _tool_calls_from_payload,
)
from .plan import PlanStore, TodoStore
from .protocol import EVENT_SCHEMA_VERSION, ArtifactRef, Event, EventKind, Op, OpKind
from .run_snapshots import PendingToolCall as SnapshotPendingToolCall
from .run_snapshots import RunSnapshot, delete_run_snapshot, read_run_snapshot, write_run_snapshot
from .skills import SkillStore
from .snapshots import GitSnapshotBackend
from .spec_workflow import SpecProposalStore, SpecStateStore, SpecStore
from .stores import ApprovalStore, ArtifactStore, EventLogStore, SessionStore
from .tool_status import normalize_tool_end_status
from .mcp.config import load_mcp_config
from .tools import (
    DAGExecuteNextTool,
    BrowserRunTool,
    ProjectApplyEditsTool,
    ProjectApplyPatchTool,
    ProjectPatchTool,
    ProjectGlobTool,
    ProjectListDirTool,
    ProjectReadTextManyTool,
    ProjectReadTextTool,
    ProjectSearchTextTool,
    ProjectTextStatsTool,
    SessionExportTool,
    SessionSearchTool,
    ShellRunTool,
    SkillListTool,
    SkillLoadTool,
    SkillReadFileTool,
    SnapshotCreateTool,
    SnapshotDiffTool,
    SnapshotListTool,
    SnapshotReadTextTool,
    SnapshotRollbackTool,
    SpecApplyTool,
    SpecGetTool,
    SpecProposeTool,
    SpecQueryTool,
    SpecSealTool,
    ToolRegistry,
    UpdatePlanTool,
    UpdateTodoTool,
)
from .tools.runtime import (
    InspectionDecision,
    PlannedToolCall,
    ToolExecutionContext,
    ToolRuntime,
    _classify_tool_exception,
    _classify_tool_result,
    _status_from_error_code,
    file_edit_ui_details,
)
from .prompts.template import render_prompt_template

from .engine import PendingToolCall, RunResult, ToolDecision

logger = logging.getLogger(__name__)


def _load_default_system_prompt() -> str:
    try:
        import importlib.resources

        return (
            importlib.resources.files("aura.runtime")
            .joinpath("prompts/system_main.md")
            .read_text(encoding="utf-8", errors="replace")
        )
    except Exception:
        return "You are Aura, a terminal-based agent.\n"


def _normalize_tool_calls(tool_calls: list[ToolCall] | None) -> list[ToolCall]:
    """
    Normalize provider tool calls for Aura's tool loop.

    - Ensure every tool call has a stable id (required for tool result linking).
    - Strip provider-specific prefixes from tool names (e.g. "default_api:tool__name").
    """

    out: list[ToolCall] = []
    for tc in tool_calls or []:
        call_id = tc.tool_call_id
        call_id = call_id.strip() if isinstance(call_id, str) and call_id.strip() else new_tool_call_id()
        name = tc.name.split(":", 1)[-1].strip()
        out.append(
            ToolCall(
                tool_call_id=call_id,
                name=name,
                arguments=dict(tc.arguments),
                raw_arguments=tc.raw_arguments,
                thought_signature=tc.thought_signature,
            )
        )
    return out


def _clean_string_list(raw: Any, *, limit_items: int = 50, item_max_len: int = 240) -> list[str]:
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, str):
            continue
        s = " ".join(item.split()).strip()
        if not s:
            continue
        if len(s) > item_max_len:
            s = s[: item_max_len - 1] + "…"
        out.append(s)
        if len(out) >= limit_items:
            break
    return out


def _public_work_spec_from_args(args: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(args, dict):
        return None
    ws = args.get("work_spec")
    if not isinstance(ws, dict):
        return None

    out: dict[str, Any] = {}

    goal = ws.get("goal")
    if isinstance(goal, str):
        goal_clean = " ".join(goal.split()).strip()
        if goal_clean:
            if len(goal_clean) > 400:
                goal_clean = goal_clean[:399] + "…"
            out["goal"] = goal_clean

    expected_raw = ws.get("expected_outputs")
    if isinstance(expected_raw, list):
        expected_outputs: list[dict[str, str]] = []
        for item in expected_raw:
            if not isinstance(item, dict):
                continue
            rec: dict[str, str] = {}
            for key, max_len in (("type", 80), ("format", 80), ("path", 320)):
                v = item.get(key)
                if not isinstance(v, str):
                    continue
                s = " ".join(v.split()).strip()
                if not s:
                    continue
                if len(s) > max_len:
                    s = s[: max_len - 1] + "…"
                rec[key] = s
            if rec:
                expected_outputs.append(rec)
            if len(expected_outputs) >= 50:
                break
        if expected_outputs:
            out["expected_outputs"] = expected_outputs

    scope_raw = ws.get("resource_scope")
    if isinstance(scope_raw, dict):
        scope: dict[str, Any] = {}
        roots = _clean_string_list(scope_raw.get("workspace_roots"), limit_items=50, item_max_len=240)
        domains = _clean_string_list(scope_raw.get("domain_allowlist"), limit_items=50, item_max_len=240)
        ftypes = _clean_string_list(scope_raw.get("file_type_allowlist"), limit_items=50, item_max_len=120)
        if roots:
            scope["workspace_roots"] = roots
        if domains:
            scope["domain_allowlist"] = domains
        if ftypes:
            scope["file_type_allowlist"] = ftypes
        if scope:
            out["resource_scope"] = scope

    inputs_raw = ws.get("inputs")
    if isinstance(inputs_raw, list):
        inputs: list[dict[str, str]] = []
        for item in inputs_raw:
            if not isinstance(item, dict):
                continue
            rec: dict[str, str] = {}
            for key, max_len in (("type", 80), ("path", 320), ("description", 320)):
                v = item.get(key)
                if not isinstance(v, str):
                    continue
                s = " ".join(v.split()).strip()
                if not s:
                    continue
                if len(s) > max_len:
                    s = s[: max_len - 1] + "…"
                rec[key] = s
            if rec:
                inputs.append(rec)
            if len(inputs) >= 50:
                break
        if inputs:
            out["inputs"] = inputs

    return out or None


_DEFAULT_EXPOSED_TOOL_NAMES: set[str] = {
    # Project filesystem (read + navigate)
    "project__read_text",
    "project__read_text_many",
    "project__search_text",
    "project__list_dir",
    "project__glob",
    "project__text_stats",
    # Project filesystem (write)
    "project__apply_edits",
    "project__patch",
    # System / network (approval-gated)
    "shell__run",
    # Browser automation (approval-gated per-step via ToolRuntime inspection)
    "browser__run",

    # Session / skills / planning
    "session__search",
    "session__export",
    "skill__list",
    "skill__load",
    "skill__read_file",
    "update_plan",
    "update_todo",
    # Spec workflow
    "spec__query",
    "spec__get",
    "spec__propose",
    "spec__apply",
    "spec__seal",
    # Subagents
    "subagent__run",
    # DAG scheduling
    "dag__execute_next",
}

_EMPTY_FINAL_RESPONSE_FALLBACK_TEXT = "Model returned an empty final response. Please retry."
_EDITABLE_APPROVAL_TOOLS: set[str] = {"project__apply_edits", "project__apply_patch", "project__patch"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(minimum, min(maximum, value))


@dataclass(slots=True)
class AgnoAsyncEngine:
    project_root: Path
    session_id: str
    event_bus: EventBus
    session_store: SessionStore
    event_log_store: EventLogStore
    artifact_store: ArtifactStore
    approval_store: ApprovalStore
    model_config: ModelConfig
    system_prompt: str | None = None
    tools_enabled: bool = False
    llm_streaming: bool = True
    max_tool_turns: int = 30
    tool_registry: ToolRegistry | None = None
    tool_runtime: ToolRuntime | None = None
    memory_summary: str | None = None
    schema_version: str = EVENT_SCHEMA_VERSION

    model_router: ModelRouter = field(init=False)
    skill_store: SkillStore = field(init=False)
    plan_store: PlanStore = field(init=False)
    todo_store: TodoStore = field(init=False)
    spec_store: SpecStore = field(init=False)
    spec_state_store: SpecStateStore = field(init=False)
    spec_proposal_store: SpecProposalStore = field(init=False)
    snapshot_backend: GitSnapshotBackend = field(init=False)

    _history: list[CanonicalMessage] | None = field(default=None, init=False)
    # Knowledge / RAG is implemented as an optional module and is not enabled by default.
    _knowledge: Any | None = field(default=None, init=False, repr=False)
    # ========== Multi-Surface extension point ==========
    # Current CLI subscribes to EventBus directly.
    # Future Web/Plugin/Cloud surfaces should follow `aura/runtime/surface.py`.
    _auto_compact_seen_turn_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _event_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _dag_runner: DAGPlanRunner | None = field(default=None, init=False, repr=False)
    _llm_network_retry_attempts: int = field(default=2, init=False, repr=False)
    _llm_network_retry_base_delay_s: float = field(default=0.75, init=False, repr=False)
    _llm_network_retry_max_delay_s: float = field(default=4.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.project_root = self.project_root.expanduser().resolve()
        self.model_router = ModelRouter(self.model_config)
        self.skill_store = SkillStore(project_root=self.project_root)
        self.plan_store = PlanStore(session_store=self.session_store, session_id=self.session_id)
        self.todo_store = TodoStore(session_store=self.session_store, session_id=self.session_id)
        self.spec_store = SpecStore(project_root=self.project_root)
        self.spec_state_store = SpecStateStore(project_root=self.project_root)
        self.spec_proposal_store = SpecProposalStore(project_root=self.project_root)
        self.snapshot_backend = GitSnapshotBackend(project_root=self.project_root)
        self._llm_network_retry_attempts = _env_int(
            "AURA_LLM_NETWORK_RETRY_ATTEMPTS",
            2,
            minimum=1,
            maximum=8,
        )
        self._llm_network_retry_base_delay_s = _env_float(
            "AURA_LLM_NETWORK_RETRY_BASE_DELAY_S",
            0.75,
            minimum=0.0,
            maximum=30.0,
        )
        self._llm_network_retry_max_delay_s = _env_float(
            "AURA_LLM_NETWORK_RETRY_MAX_DELAY_S",
            4.0,
            minimum=0.0,
            maximum=60.0,
        )

        if self.max_tool_turns < 1:
            raise ValueError("max_tool_turns must be >= 1")
        if self.max_tool_turns > 256:
            raise ValueError("max_tool_turns must be <= 256")

        registry = ToolRegistry()
        registry.register(ProjectReadTextTool())
        registry.register(ProjectApplyEditsTool())
        registry.register(ProjectApplyPatchTool())
        registry.register(ProjectPatchTool())
        registry.register(ProjectSearchTextTool())
        registry.register(ProjectListDirTool())
        registry.register(ProjectGlobTool())
        registry.register(ProjectReadTextManyTool())
        registry.register(ProjectTextStatsTool())
        registry.register(ShellRunTool())
        registry.register(BrowserRunTool(artifact_store=self.artifact_store))

        registry.register(SessionSearchTool())
        registry.register(SessionExportTool())
        registry.register(SkillListTool(self.skill_store))
        registry.register(SkillLoadTool(self.skill_store))
        registry.register(SkillReadFileTool(self.skill_store))
        registry.register(UpdatePlanTool(self.plan_store))
        registry.register(UpdateTodoTool(self.todo_store))
        registry.register(SpecQueryTool(self.spec_store))
        registry.register(SpecGetTool(self.spec_store))
        registry.register(
            SpecProposeTool(self.spec_store, self.spec_proposal_store, self.spec_state_store, self.artifact_store)
        )
        registry.register(SpecApplyTool(self.spec_proposal_store, self.spec_state_store))
        registry.register(SpecSealTool(self.spec_state_store, self.snapshot_backend))
        registry.register(SnapshotListTool(self.snapshot_backend))
        registry.register(SnapshotCreateTool(self.snapshot_backend))
        registry.register(SnapshotReadTextTool(self.snapshot_backend))
        registry.register(SnapshotDiffTool(self.snapshot_backend))
        registry.register(SnapshotRollbackTool(self.snapshot_backend))

        tool_runtime = ToolRuntime(project_root=self.project_root, registry=registry, artifact_store=self.artifact_store)

        try:
            from .tools.subagent_runner import SubagentRunTool

            subagent_tool = SubagentRunTool(
                model_router=self.model_router,
                tool_registry=registry,
                tool_runtime=tool_runtime,
                artifact_store=self.artifact_store,
            )
            registry.register(subagent_tool)

            dag_runner = DAGPlanRunner(plan_store=self.plan_store, max_parallel=3)
            self._dag_runner = dag_runner
            registry.register(DAGExecuteNextTool(dag_runner=dag_runner, subagent_tool=subagent_tool))
        except Exception:
            self._dag_runner = None

        self.tool_registry = registry
        self.tool_runtime = tool_runtime

        # Initialize event sequence from the last persisted event (best-effort).
        last = 0
        try:
            for evt in self.event_log_store.read(self.session_id):
                if isinstance(evt.sequence, int) and evt.sequence > last:
                    last = evt.sequence
        except Exception:
            last = 0
        self.event_bus.prime_sequence(session_id=self.session_id, last_sequence=last)

    def get_llm_streaming(self) -> bool:
        return bool(self.llm_streaming)

    def set_llm_streaming(self, enabled: bool) -> None:
        self.llm_streaming = bool(enabled)

    def set_chat_model_profile(self, profile_id: str) -> None:
        if profile_id not in self.model_config.profiles:
            raise ValueError(f"Unknown model profile: {profile_id}")
        cfg = ModelConfig(
            profiles=dict(self.model_config.profiles),
            role_pointers={ModelRole.MAIN: profile_id},
        )
        cfg.validate_consistency()
        self.model_config = cfg
        # Preserve the ModelRouter instance so existing tool instances (e.g. subagent__run)
        # observe model switches without being re-registered.
        if self.model_router is None:
            self.model_router = ModelRouter(cfg)
        else:
            self.model_router.set_config(cfg)

    def load_history_from_events(self) -> None:
        history: list[CanonicalMessage] = []

        def _read_artifact_text(ref_dict: dict[str, Any]) -> str:
            ref = ArtifactRef.from_dict(ref_dict)
            data = self.artifact_store.get(ref)
            return data.decode("utf-8", errors="replace")

        for event in self.event_log_store.read(self.session_id):
            if event.kind == EventKind.OPERATION_STARTED.value:
                ref_raw = event.payload.get("input_ref")
                if isinstance(ref_raw, dict):
                    text = _read_artifact_text(ref_raw)
                    history.append(CanonicalMessage(role=CanonicalMessageRole.USER, content=text))
            elif event.kind == EventKind.LLM_RESPONSE_COMPLETED.value:
                subagent_run_id = event.payload.get("subagent_run_id")
                if isinstance(subagent_run_id, str) and subagent_run_id:
                    continue
                if event.payload.get("op_kind") == OpKind.COMPACT.value:
                    # Compaction runs are internal and must not be rehydrated into chat history.
                    continue
                ref_raw = event.payload.get("output_ref")
                if isinstance(ref_raw, dict):
                    text = _read_artifact_text(ref_raw)
                    tool_calls = _tool_calls_from_payload(
                        event.payload.get("tool_calls"),
                        read_artifact_text=_read_artifact_text,
                    )
                    history.append(
                        CanonicalMessage(
                            role=CanonicalMessageRole.ASSISTANT,
                            content=text,
                            tool_calls=tool_calls or None,
                        )
                    )
            elif event.kind == EventKind.TOOL_CALL_END.value:
                subagent_run_id = event.payload.get("subagent_run_id")
                if isinstance(subagent_run_id, str) and subagent_run_id:
                    continue
                payload = event.payload
                tool_call_id = payload.get("tool_call_id")
                tool_name = payload.get("tool_name")
                ref_raw = payload.get("tool_message_ref")
                if (
                    isinstance(tool_call_id, str)
                    and tool_call_id
                    and isinstance(tool_name, str)
                    and tool_name
                    and isinstance(ref_raw, dict)
                ):
                    content = _read_artifact_text(ref_raw)
                    history.append(
                        CanonicalMessage(
                            role=CanonicalMessageRole.TOOL,
                            content=content,
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                        )
                    )

        self._history = history

    def _maybe_repair_interrupted_turn(self) -> dict[str, Any] | None:
        """
        Repair persisted history that ends mid-tool-turn.

        This can happen if the process is interrupted (crash / Ctrl+C) after a tool executes but
        before the model produces the follow-up assistant message. Some model providers reject
        histories where the user speaks before the assistant finishes the tool turn.

        Strategy: do NOT delete history. Instead, "close" the dangling tool turn by appending
        synthetic tool/assistant messages to make the conversation structure valid for providers.
        """
        if not self._history:
            return None

        history = self._history

        # Case A: history ends with tool messages.
        if history[-1].role is CanonicalMessageRole.TOOL:
            first_tool_idx = len(history) - 1
            while first_tool_idx > 0 and history[first_tool_idx - 1].role is CanonicalMessageRole.TOOL:
                first_tool_idx -= 1
            assistant_idx = first_tool_idx - 1
            if (
                assistant_idx >= 0
                and history[assistant_idx].role is CanonicalMessageRole.ASSISTANT
                and history[assistant_idx].tool_calls
            ):
                tool_names = sorted(
                    {m.tool_name for m in history[first_tool_idx:] if m.role is CanonicalMessageRole.TOOL and isinstance(m.tool_name, str) and m.tool_name}
                )
                summary = ", ".join(tool_names[:4])
                if len(tool_names) > 4:
                    summary = f"{summary} (+{len(tool_names) - 4} more)"
                note = "Recovery note: previous run ended after tool execution."
                if summary:
                    note = f"{note} Tool results recorded: {summary}."
                else:
                    note = f"{note} Tool results were recorded."
                self._history.append(CanonicalMessage(role=CanonicalMessageRole.ASSISTANT, content=note))
                return {
                    "type": "history_repair",
                    "reason": "dangling_tool_turn",
                    "action": "append_assistant_close",
                    "appended_messages": 1,
                    "appended_tool_messages": 0,
                    "tool_names": tool_names,
                }

        # Case B: history ends with an assistant tool call (no tool response persisted yet).
        if history[-1].role is CanonicalMessageRole.ASSISTANT and history[-1].tool_calls:
            tool_calls = list(history[-1].tool_calls or [])
            appended_tools = 0
            tool_names: list[str] = []
            for tc in tool_calls:
                tool_call_id = getattr(tc, "tool_call_id", None)
                tool_name = getattr(tc, "name", None)
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    continue
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                tool_names.append(tool_name)
                tool_message = json.dumps(
                    {
                        "ok": False,
                        "tool": tool_name,
                        "error_code": ErrorCode.CANCELLED.value,
                        "error": "Interrupted before tool execution; marking as cancelled for history consistency.",
                        "result": None,
                    },
                    ensure_ascii=False,
                )
                self._history.append(
                    CanonicalMessage(
                        role=CanonicalMessageRole.TOOL,
                        content=tool_message,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                    )
                )
                appended_tools += 1

            tool_names_sorted = sorted({n for n in tool_names if n})
            note = (
                "Recovery note: previous run ended before tool results were recorded. "
                "Pending tool calls were marked as cancelled; re-run them if still needed."
            )
            self._history.append(CanonicalMessage(role=CanonicalMessageRole.ASSISTANT, content=note))
            return {
                "type": "history_repair",
                "reason": "dangling_tool_turn",
                "action": "append_cancelled_tools_and_close",
                "appended_messages": appended_tools + 1,
                "appended_tool_messages": appended_tools,
                "tool_names": tool_names_sorted,
            }

        return None

    def apply_memory_summary_retention(self) -> None:
        if self._history is None:
            self._history = []
        if not (isinstance(self.memory_summary, str) and self.memory_summary.strip()):
            return
        profile = self.model_config.get_profile_for_role(ModelRole.MAIN)
        if profile is None:
            return
        try:
            from .compaction import apply_compaction_retention, settings_for_profile
        except Exception:
            return
        cm = settings_for_profile(profile)
        context_limit_tokens = resolve_context_limit_tokens(
            profile.limits.context_limit_tokens if profile.limits is not None else None
        )
        retained = apply_compaction_retention(
            history=list(self._history),
            memory_summary=self.memory_summary.strip(),
            context_limit_tokens=context_limit_tokens,
            history_budget_ratio=cm.history_budget_ratio,
            history_budget_fallback_tokens=cm.history_budget_fallback_tokens,
        )
        self.memory_summary = retained.memory_summary
        self._history = list(retained.retained_history)

    async def arun(
        self,
        op: Op,
        *,
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
    ) -> RunResult:
        if op.session_id != self.session_id:
            raise ValueError("Op session_id does not match engine session.")

        if op.kind == OpKind.APPROVAL_DECISION.value:
            return await self._arun_approval_decision(op, timeout_s=timeout_s, cancel=cancel)

        pending = self.approval_store.list(session_id=self.session_id, status=ApprovalStatus.PENDING)
        if pending:
            return RunResult(
                status="needs_approval",
                run_id=str(pending[0].request_id),
                session_id=self.session_id,
                approval_id=pending[0].approval_id,
                pending_tools=[],
                error="Session has pending approvals.",
            )

        if op.kind == OpKind.CHAT.value:
            return await self._arun_chat(op, timeout_s=timeout_s, cancel=cancel)

        if op.kind == OpKind.COMPACT.value:
            ok = await self._perform_compaction(
                trigger="manual",
                request_id=op.request_id,
                turn_id=op.turn_id,
                timeout_s=timeout_s,
                cancel=cancel,
                extra_tools=None,
            )
            if ok:
                return RunResult(status="completed", run_id=op.request_id, session_id=self.session_id)
            return RunResult(status="failed", run_id=op.request_id, session_id=self.session_id, error="compact_failed")

        await self._emit(
            kind=EventKind.OPERATION_FAILED,
            payload={"error": f"Unsupported op kind: {op.kind}", "error_code": ErrorCode.BAD_REQUEST.value},
            request_id=op.request_id,
            turn_id=op.turn_id,
            step_id=None,
        )
        return RunResult(status="failed", run_id=op.request_id, session_id=self.session_id, error="unsupported_op")

    def run(
        self,
        op: Op,
        *,
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
    ) -> RunResult:
        """
        Synchronous wrapper used by the current CLI.

        Async surfaces should call `await engine.arun(...)` directly.
        """
        return asyncio.run(self.arun(op, timeout_s=timeout_s, cancel=cancel))

    async def continue_run(
        self,
        *,
        run_id: str,
        decisions: list[ToolDecision],
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
    ) -> RunResult:
        snapshot = read_run_snapshot(project_root=self.project_root, run_id=run_id)
        if snapshot.session_id != self.session_id:
            raise ValueError("Run snapshot session mismatch.")
        if snapshot.model_profile_id:
            try:
                self.set_chat_model_profile(snapshot.model_profile_id)
            except Exception:
                logger.warning("Suppressed exception in continue_run.", exc_info=True)
        self._history = list(snapshot.messages)
        turn_id = snapshot.turn_id

        decision_map: dict[str, ToolDecision] = {d.tool_call_id: d for d in decisions if d.tool_call_id}
        pending = list(snapshot.pending_tools)

        approval_record: ApprovalRecord | None = None
        approval_event_kind: EventKind | None = None
        approval_event_decision: str | None = None
        approval_event_details: dict[str, Any] | None = None
        # Clear approvals if all required decisions are present.
        approval_id = snapshot.approval_id
        if approval_id:
            try:
                approval_record = self.approval_store.get(approval_id)
                if approval_record.status is ApprovalStatus.PENDING:
                    record_decision = {
                        "decisions": [
                            {"tool_call_id": d.tool_call_id, "decision": d.decision, "note": d.note}
                            for d in decisions
                        ],
                        "decided_at": now_ts_ms(),
                    }
                    status = ApprovalStatus.GRANTED if any(d.decision == "approve" for d in decisions) else ApprovalStatus.DENIED
                    updated = replace(approval_record, status=status, decision=record_decision)
                    self.approval_store.update(updated)
                    approval_record = updated
                    approval_event_kind = EventKind.APPROVAL_GRANTED if status is ApprovalStatus.GRANTED else EventKind.APPROVAL_DENIED
                    approval_event_decision = "approve" if status is ApprovalStatus.GRANTED else "deny"
                    approval_event_details = record_decision
            except Exception:
                logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)

        if approval_record is not None and approval_event_kind is not None and approval_id:
            await self._emit(
                kind=approval_event_kind,
                payload={
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "decision": approval_event_decision,
                    "details": (approval_event_details or {}),
                },
                request_id=run_id,
                turn_id=turn_id,
                step_id=None,
            )
            if approval_event_kind is EventKind.APPROVAL_GRANTED:
                await self._emit(
                    kind=EventKind.RUN_RESUMED,
                    payload={
                        "run_id": run_id,
                        "approval_id": approval_id,
                        "decision": "approve",
                        "pending_tools_count": len(pending),
                    },
                    request_id=run_id,
                    turn_id=turn_id,
                    step_id=None,
                )

        # If this approval originated from a subagent run, auto-resume the delegated work
        # after the user approves, so the subagent continues without requiring the main agent
        # to manually re-dispatch it.
        if (
            approval_record is not None
            and isinstance(approval_record.resume_payload, dict)
            and approval_record.resume_payload.get("source") == "subagent"
            and any(d.decision == "approve" for d in decisions)
        ):
            await self._auto_resume_subagent_after_approval(
                approval_record=approval_record,
                request_id=run_id,
                turn_id=turn_id,
                pending_tools=pending,
                decision_map=decision_map,
                timeout_s=timeout_s,
                cancel=cancel,
            )
            # We already executed the approved tool calls (and re-dispatched the subagent).
            pending = []
            decision_map = {}

        # For DAG browser-takeover pauses, approval means the human has completed
        # CAPTCHA/login and we should re-dispatch `dag__execute_next` immediately.
        if (
            approval_record is not None
            and isinstance(approval_record.resume_payload, dict)
            and approval_record.resume_payload.get("source") == "dag"
            and any(d.decision == "approve" for d in decisions)
        ):
            dag_payload = approval_record.resume_payload.get("dag")
            if isinstance(dag_payload, dict) and dag_payload.get("takeover") is True:
                await self._auto_resume_dag_after_takeover(
                    approval_record=approval_record,
                    request_id=run_id,
                    turn_id=turn_id,
                    timeout_s=timeout_s,
                    cancel=cancel,
                )
                pending = []
                decision_map = {}

        result = await self._run_tool_loop(
            request_id=run_id,
            turn_id=turn_id,
            pending_tools=(pending if pending else None),
            decision_map=decision_map,
            timeout_s=timeout_s,
            cancel=cancel,
        )
        if result.status != "needs_approval":
            delete_run_snapshot(project_root=self.project_root, run_id=run_id)
        return result

    async def _auto_resume_subagent_after_approval(
        self,
        *,
        approval_record: ApprovalRecord,
        request_id: str,
        turn_id: str | None,
        pending_tools: list[SnapshotPendingToolCall],
        decision_map: dict[str, ToolDecision],
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> None:
        """
        Execute the approved tool calls, then re-dispatch the original subagent run with a resume hint.

        This provides the UX the user expects: approve -> delegated worker continues.
        """
        cancel = cancel or CancellationToken()
        if cancel.cancelled:
            return
        if self.tool_registry is None or self.tool_runtime is None:
            return
        if self._history is None:
            self._history = []

        sub_payload = approval_record.resume_payload.get("subagent") if isinstance(approval_record.resume_payload, dict) else None
        if not isinstance(sub_payload, dict):
            return
        run_args = sub_payload.get("run_args")
        if not isinstance(run_args, dict) or not run_args:
            return

        executed: list[dict[str, Any]] = []

        # The snapshot's history may end with an ASSISTANT message whose tool_calls were never
        # acknowledged (e.g. dag__execute_next was still in-flight when the run was paused for
        # approval). Without a synthetic TOOL result for each such call, the conversation is
        # malformed and the main LLM will produce stale output (e.g. still saying "please approve"
        # even though approval was granted and the browser already ran).
        if self._history:
            last_hist_msg = self._history[-1]
            if last_hist_msg.role == CanonicalMessageRole.ASSISTANT and last_hist_msg.tool_calls:
                for tc in last_hist_msg.tool_calls:
                    if tc.tool_call_id:
                        self._history.append(
                            CanonicalMessage(
                                role=CanonicalMessageRole.TOOL,
                                content=json.dumps({
                                    "status": "resumed",
                                    "note": (
                                        "A tool call within this subagent required user approval. "
                                        "Approval was granted. The approved tool call is now being "
                                        "executed and the subagent will be re-dispatched with the result."
                                    ),
                                }),
                                tool_call_id=tc.tool_call_id,
                                tool_name=tc.name,
                            )
                        )

        async with AsyncExitStack() as stack:
            mcp_functions, _mcp_specs = await self._load_mcp_tooling(stack=stack)

            # 1) Execute the approved pending tool calls (without an extra main-agent LLM turn).
            for t in list(pending_tools):
                if cancel.cancelled:
                    return
                tool_call_id = t.tool_call_id
                # These approved tool calls originate from a paused approval, not from an LLM tool-call turn.
                # To keep downstream providers happy (and to make the transcript auditable), we synthesize
                # an assistant tool-call message for each approved tool call before emitting the tool result.
                self._history.append(
                    CanonicalMessage(
                        role=CanonicalMessageRole.ASSISTANT,
                        content="",
                        tool_calls=[ToolCall(tool_call_id=tool_call_id, name=t.tool_name, arguments=deepcopy(t.args))],
                    )
                )
                planned = self.tool_runtime.plan(
                    tool_execution_id=f"tool_{tool_call_id}",
                    tool_name=t.tool_name,
                    tool_call_id=tool_call_id,
                    arguments=dict(t.args),
                )
                inspection = self._inspect_tool(planned=planned, mcp_functions=mcp_functions)
                decision = decision_map.get(tool_call_id)
                tool_message = await self._execute_planned_after_decisions(
                    planned=planned,
                    inspection=inspection,
                    decision=decision,
                    request_id=request_id,
                    turn_id=turn_id,
                    mcp_functions=mcp_functions,
                )
                self._history.append(
                    CanonicalMessage(
                        role=CanonicalMessageRole.TOOL,
                        content=tool_message,
                        tool_call_id=tool_call_id,
                        tool_name=t.tool_name,
                    )
                )
                executed.append(
                    {
                        "tool_name": t.tool_name,
                        "tool_call_id": tool_call_id,
                        "args": dict(t.args),
                        # Keep as text; the subagent can decide what to do next.
                        "tool_message": tool_message,
                    }
                )

            # 2) Re-dispatch the subagent with a resume hint that includes the approved tool outcomes.
            resume_args = deepcopy(run_args)
            ctx = resume_args.get("context")
            if not isinstance(ctx, dict):
                ctx = {}
            text = ctx.get("text")
            if not isinstance(text, str):
                text = ""
            hint = {
                "kind": "approval_resume",
                "approved_tools": executed,
                "note": "User approved the previously blocked tool call(s). Continue the delegated work. Do not ask the user again for the same approval; proceed using the current workspace state.",
            }
            ctx["resume"] = hint
            ctx["text"] = (text + "\n\n[Approval resume]\nThe user approved the previously blocked tool call(s). Continue.").strip()
            resume_args["context"] = ctx

            call_id = new_tool_call_id()
            # Add a synthetic assistant tool-call message so the next TOOL message is well-formed.
            self._history.append(
                CanonicalMessage(
                    role=CanonicalMessageRole.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(tool_call_id=call_id, name="subagent__run", arguments=deepcopy(resume_args))],
                )
            )
            planned_sub = self.tool_runtime.plan(
                tool_execution_id=f"tool_{call_id}",
                tool_name="subagent__run",
                tool_call_id=call_id,
                arguments=deepcopy(resume_args),
            )
            inspection_sub = self._inspect_tool(planned=planned_sub, mcp_functions=mcp_functions)
            tool_message_sub = await self._execute_planned_after_decisions(
                planned=planned_sub,
                inspection=inspection_sub,
                decision=None,
                request_id=request_id,
                turn_id=turn_id,
                mcp_functions=mcp_functions,
            )
            self._history.append(
                CanonicalMessage(
                    role=CanonicalMessageRole.TOOL,
                    content=tool_message_sub,
                    tool_call_id=call_id,
                    tool_name="subagent__run",
                )
            )


    async def _auto_resume_dag_after_takeover(
        self,
        *,
        approval_record: ApprovalRecord,
        request_id: str,
        turn_id: str | None,
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> None:
        cancel = cancel or CancellationToken()
        if cancel.cancelled:
            return
        if self.tool_registry is None or self.tool_runtime is None:
            return
        if self._history is None:
            self._history = []

        payload = approval_record.resume_payload if isinstance(approval_record.resume_payload, dict) else None
        if not isinstance(payload, dict):
            return
        dag_payload = payload.get("dag")
        if not isinstance(dag_payload, dict):
            return
        if dag_payload.get("takeover") is not True:
            return

        run_args = dag_payload.get("dag_execute_args")
        if not isinstance(run_args, dict):
            run_args = {}

        # Same orphaned-tool-call fix as in _auto_resume_subagent_after_approval: the snapshot
        # history may end with an unresolved ASSISTANT tool_call. Inject a synthetic TOOL result
        # before appending the new dag__execute_next call so the conversation stays well-formed.
        if self._history:
            last_hist_msg = self._history[-1]
            if last_hist_msg.role == CanonicalMessageRole.ASSISTANT and last_hist_msg.tool_calls:
                for tc in last_hist_msg.tool_calls:
                    if tc.tool_call_id:
                        self._history.append(
                            CanonicalMessage(
                                role=CanonicalMessageRole.TOOL,
                                content=json.dumps({
                                    "status": "resumed",
                                    "note": (
                                        "The browser session was handed over to the user (e.g. for CAPTCHA/login). "
                                        "User completed the handover. Resuming DAG execution now."
                                    ),
                                }),
                                tool_call_id=tc.tool_call_id,
                                tool_name=tc.name,
                            )
                        )

        async with AsyncExitStack() as stack:
            mcp_functions, _mcp_specs = await self._load_mcp_tooling(stack=stack)

            call_id = new_tool_call_id()
            self._history.append(
                CanonicalMessage(
                    role=CanonicalMessageRole.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(tool_call_id=call_id, name="dag__execute_next", arguments=deepcopy(run_args))],
                )
            )

            planned = self.tool_runtime.plan(
                tool_execution_id=f"tool_{call_id}",
                tool_name="dag__execute_next",
                tool_call_id=call_id,
                arguments=deepcopy(run_args),
            )
            inspection = self._inspect_tool(planned=planned, mcp_functions=mcp_functions)
            tool_message = await self._execute_planned_after_decisions(
                planned=planned,
                inspection=inspection,
                decision=None,
                request_id=request_id,
                turn_id=turn_id,
                mcp_functions=mcp_functions,
            )
            self._history.append(
                CanonicalMessage(
                    role=CanonicalMessageRole.TOOL,
                    content=tool_message,
                    tool_call_id=call_id,
                    tool_name="dag__execute_next",
                )
            )

    async def _arun_chat(
        self,
        op: Op,
        *,
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> RunResult:
        user_text = str(op.payload.get("text") or "")
        if not user_text.strip():
            await self._emit(
                kind=EventKind.OPERATION_FAILED,
                payload={"error": "Empty input.", "error_code": ErrorCode.BAD_REQUEST.value},
                request_id=op.request_id,
                turn_id=op.turn_id,
                step_id=None,
            )
            return RunResult(status="failed", run_id=op.request_id, session_id=self.session_id, error="empty_input")

        if self._history is None:
            self._history = []

        repair_info = self._maybe_repair_interrupted_turn()
        input_ref = self.artifact_store.put(user_text, kind="chat_user", meta={"summary": _summarize_text(user_text)})
        await self._emit(
            kind=EventKind.OPERATION_STARTED,
            payload={"op_kind": OpKind.CHAT.value, "input_ref": input_ref.to_dict()},
            request_id=op.request_id,
            turn_id=op.turn_id,
            step_id=None,
        )
        if repair_info is not None:
            await self._emit(
                kind=EventKind.OPERATION_PROGRESS,
                payload=repair_info,
                request_id=op.request_id,
                turn_id=op.turn_id,
                step_id=None,
            )
        self._history.append(CanonicalMessage(role=CanonicalMessageRole.USER, content=user_text))

        return await self._run_tool_loop(
            request_id=op.request_id,
            turn_id=op.turn_id,
            pending_tools=None,
            decision_map={},
            timeout_s=timeout_s,
            cancel=cancel,
        )

    async def _arun_approval_decision(
        self,
        op: Op,
        *,
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> RunResult:
        approval_id = str(op.payload.get("approval_id") or "")
        decision_raw = str(op.payload.get("decision") or "")
        note = op.payload.get("note")

        if not approval_id:
            return RunResult(status="failed", run_id=op.request_id, session_id=self.session_id, error="missing_approval_id")

        try:
            record = self.approval_store.get(approval_id)
        except FileNotFoundError:
            return RunResult(status="failed", run_id=op.request_id, session_id=self.session_id, error="approval_not_found")

        if record.session_id != self.session_id:
            return RunResult(status="failed", run_id=op.request_id, session_id=self.session_id, error="approval_session_mismatch")

        if record.status is not ApprovalStatus.PENDING:
            return RunResult(status="failed", run_id=record.request_id, session_id=self.session_id, error="approval_not_pending")

        try:
            decision = ApprovalDecision(decision_raw)
        except ValueError:
            return RunResult(status="failed", run_id=record.request_id, session_id=self.session_id, error="approval_decision_invalid")

        tool_calls = record.resume_payload.get("tool_calls") if isinstance(record.resume_payload, dict) else None
        pending_ids: list[str] = []
        pending_by_id: dict[str, dict[str, Any]] = {}
        if isinstance(tool_calls, list):
            for c in tool_calls:
                if not isinstance(c, dict):
                    continue
                tid = c.get("tool_call_id")
                if isinstance(tid, str) and tid:
                    pending_ids.append(tid)
                    pending_by_id[tid] = dict(c)

        if decision is ApprovalDecision.APPROVE:
            decisions = [ToolDecision(tool_call_id=tid, decision="approve") for tid in pending_ids]
        elif decision is ApprovalDecision.DENY:
            decisions = [ToolDecision(tool_call_id=tid, decision="deny", note=str(note) if note is not None else None) for tid in pending_ids]
        else:
            # edit / dry_run are only supported for a single pending editable tool call.
            if len(pending_ids) != 1:
                return RunResult(
                    status="failed",
                    run_id=record.request_id,
                    session_id=self.session_id,
                    error="approval_decision_unsupported_for_batch",
                )

            target_id = pending_ids[0]
            target_call = pending_by_id.get(target_id) or {}
            tool_name = str(target_call.get("tool_name") or "")
            if tool_name not in _EDITABLE_APPROVAL_TOOLS:
                return RunResult(
                    status="failed",
                    run_id=record.request_id,
                    session_id=self.session_id,
                    error="approval_decision_unsupported_tool",
                )

            edited_arguments_raw = op.payload.get("edited_arguments")
            if decision is ApprovalDecision.EDIT:
                if not isinstance(edited_arguments_raw, dict):
                    return RunResult(
                        status="failed",
                        run_id=record.request_id,
                        session_id=self.session_id,
                        error="approval_edit_arguments_missing",
                    )
                edited_arguments = dict(edited_arguments_raw)
            else:
                # dry_run: start from current args and force dry_run=true.
                edited_arguments = {}
                try:
                    snapshot = read_run_snapshot(project_root=self.project_root, run_id=record.request_id)
                    for pending in snapshot.pending_tools:
                        if pending.tool_call_id == target_id:
                            edited_arguments = dict(pending.args or {})
                            break
                except Exception:
                    logger.warning("Failed to read run snapshot while building dry_run edited arguments.", exc_info=True)
                    edited_arguments = {}
                edited_arguments["dry_run"] = True

            try:
                snapshot = read_run_snapshot(project_root=self.project_root, run_id=record.request_id)
            except Exception:
                logger.warning("Failed to read run snapshot for approval edit/dry_run.", exc_info=True)
                return RunResult(
                    status="failed",
                    run_id=record.request_id,
                    session_id=self.session_id,
                    error="approval_snapshot_not_found",
                )

            updated_pending: list[SnapshotPendingToolCall] = []
            for pending in snapshot.pending_tools:
                if pending.tool_call_id == target_id:
                    updated_pending.append(
                        SnapshotPendingToolCall(
                            tool_call_id=pending.tool_call_id,
                            tool_name=pending.tool_name,
                            args=edited_arguments,
                        )
                    )
                else:
                    updated_pending.append(pending)

            write_run_snapshot(
                project_root=self.project_root,
                snapshot=replace(snapshot, pending_tools=updated_pending),
            )
            edit_note = str(note).strip() if note is not None else ""
            if decision is ApprovalDecision.DRY_RUN:
                note_text = "dry_run requested via approval UI."
                if edit_note:
                    note_text = f"{note_text} {edit_note}"
            else:
                note_text = "arguments edited via approval UI."
                if edit_note:
                    note_text = f"{note_text} {edit_note}"
            decisions = [ToolDecision(tool_call_id=target_id, decision="approve", note=note_text)]

        return await self.continue_run(run_id=record.request_id, decisions=decisions, timeout_s=timeout_s, cancel=cancel)

    async def _run_tool_loop(
        self,
        *,
        request_id: str,
        turn_id: str | None,
        pending_tools: list[SnapshotPendingToolCall] | None,
        decision_map: dict[str, ToolDecision],
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> RunResult:
        cancel = cancel or CancellationToken()
        if cancel.cancelled:
            await self._emit(
                kind=EventKind.OPERATION_CANCELLED,
                payload={"op_kind": OpKind.CHAT.value, "error_code": ErrorCode.CANCELLED.value, "reason": "cancelled"},
                request_id=request_id,
                turn_id=turn_id,
                step_id=None,
            )
            return RunResult(status="cancelled", run_id=request_id, session_id=self.session_id, error="cancelled")

        if self._history is None:
            self._history = []

        if self.tool_registry is None or self.tool_runtime is None:
            raise RuntimeError("Tool runtime not initialized.")

        profile = None
        try:
            reqs = ModelRequirements(needs_tools=self.tools_enabled)
            resolved = self.model_router.resolve(role=ModelRole.MAIN, requirements=reqs)
            profile = resolved.profile
        except ModelResolutionError as e:
            await self._emit(
                kind=EventKind.OPERATION_FAILED,
                payload={"error": str(e), "error_code": ErrorCode.MODEL_RESOLUTION.value, "type": "model_resolution"},
                request_id=request_id,
                turn_id=turn_id,
                step_id=None,
            )
            return RunResult(status="failed", run_id=request_id, session_id=self.session_id, error="model_resolution")

        async with AsyncExitStack() as stack:
            mcp_functions, mcp_specs = await self._load_mcp_tooling(stack=stack)

            guard_id = str(turn_id or request_id)
            while True:
                # Build system prompt and tool surface (Aura tools + MCP tools).
                request = self._build_request(profile=profile, extra_tools=mcp_specs)
                context_limit_tokens = resolve_context_limit_tokens(
                    profile.limits.context_limit_tokens if profile.limits is not None else None
                )
                estimated_input_tokens = approx_tokens_from_json(canonical_request_to_dict(request))
                context_stats: dict[str, Any] = {
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimate_kind": "bytes_per_token_4",
                    "context_limit_tokens": context_limit_tokens,
                }
                if isinstance(context_limit_tokens, int) and context_limit_tokens > 0:
                    context_stats["estimated_context_left_percent"] = compute_context_left_percent(
                        used_tokens=estimated_input_tokens,
                        context_limit_tokens=context_limit_tokens,
                    )

                # Auto-compaction guard (no extra "magic": only triggers via explicit config thresholds).
                try:
                    from .compaction import settings_for_profile, should_auto_compact

                    cm = settings_for_profile(profile)
                    threshold_ratio = cm.auto_compact_threshold_ratio
                    if (
                        guard_id
                        and guard_id not in self._auto_compact_seen_turn_ids
                        and should_auto_compact(
                            estimated_input_tokens=estimated_input_tokens,
                            context_limit_tokens=context_limit_tokens,
                            threshold_ratio=threshold_ratio,
                        )
                    ):
                        self._auto_compact_seen_turn_ids.add(guard_id)
                        ok = await self._perform_compaction(
                            trigger="auto",
                            request_id=request_id,
                            turn_id=turn_id,
                            timeout_s=timeout_s,
                            cancel=cancel,
                            context_stats=context_stats,
                            threshold_ratio=threshold_ratio,
                            extra_tools=mcp_specs,
                        )
                        if not ok:
                            return RunResult(status="failed", run_id=request_id, session_id=self.session_id, error="compact_failed")
                        continue
                except Exception:
                    logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)

                break

            # If we are resuming from a paused snapshot, execute those tools first.
            if pending_tools:
                pending_planned = []
                for t in pending_tools:
                    pending_planned.append(
                        self.tool_runtime.plan(
                            tool_execution_id=f"tool_{t.tool_call_id}",
                            tool_name=t.tool_name,
                            tool_call_id=t.tool_call_id,
                            arguments=dict(t.args),
                        )
                    )

                if await self._needs_more_approval(
                    request_id=request_id,
                    turn_id=turn_id,
                    planned_calls=pending_planned,
                    decision_map=decision_map,
                    mcp_functions=mcp_functions,
                    context_stats=context_stats,
                    model_profile_id=getattr(profile, "profile_id", None),
                ):
                    return RunResult(
                        status="needs_approval",
                        run_id=request_id,
                        session_id=self.session_id,
                        approval_id=read_run_snapshot(project_root=self.project_root, run_id=request_id).approval_id,
                        pending_tools=[
                            PendingToolCall(tool_call_id=p.tool_call_id, tool_name=p.tool_name, args=dict(p.arguments))
                            for p in pending_planned
                        ],
                    )

                for idx, planned in enumerate(pending_planned):
                    inspection = self._inspect_tool(planned=planned, mcp_functions=mcp_functions)
                    tool_message = await self._execute_planned_after_decisions(
                        planned=planned,
                        inspection=inspection,
                        decision=decision_map.get(planned.tool_call_id),
                        request_id=request_id,
                        turn_id=turn_id,
                        mcp_functions=mcp_functions,
                    )
                    self._history.append(
                        CanonicalMessage(
                            role=CanonicalMessageRole.TOOL,
                            content=tool_message,
                            tool_call_id=planned.tool_call_id,
                            tool_name=planned.tool_name,
                        )
                    )
                    # Fail-fast: if a tool call failed/blocked/cancelled, do not execute the rest of the tool calls
                    # from the same assistant turn. Instead, close them out as cancelled so provider adapters always
                    # have a 1:1 tool_call -> tool_result pairing.
                    try:
                        msg = json.loads(tool_message)
                    except Exception:
                        msg = None
                    if isinstance(msg, dict) and msg.get("ok") is False:
                        prev_tool = planned.tool_name
                        prev_code = msg.get("error_code") if isinstance(msg.get("error_code"), str) else None
                        prev_err = msg.get("error") if isinstance(msg.get("error"), str) else None
                        reason = f"Skipped because a previous tool call failed: {prev_tool}"
                        if prev_code:
                            reason = f"{reason} ({prev_code})"
                        if prev_err:
                            reason = f"{reason}: {prev_err}"
                        if len(reason) > 400:
                            reason = reason[:399].rstrip() + "…"

                        for remaining in pending_planned[idx + 1 :]:
                            skipped_message = await self._tool_result_error(
                                planned=remaining,
                                request_id=request_id,
                                turn_id=turn_id,
                                error_code=ErrorCode.CANCELLED.value,
                                error_message=reason,
                                status="cancelled",
                            )
                            self._history.append(
                                CanonicalMessage(
                                    role=CanonicalMessageRole.TOOL,
                                    content=skipped_message,
                                    tool_call_id=remaining.tool_call_id,
                                    tool_name=remaining.tool_name,
                                )
                            )
                        break

                # If the user denied an approval and provided guidance ("Tell assistant what to do differently"),
                # surface it as a user message *after* the tool responses so providers keep tool-call pairing valid.
                try:
                    note_clean: str | None = None
                    for planned in pending_planned:
                        d = decision_map.get(planned.tool_call_id)
                        if d is None:
                            continue
                        if d.decision == "approve":
                            continue
                        if isinstance(d.note, str) and d.note.strip():
                            note_clean = " ".join(d.note.strip().splitlines()).strip()
                            break
                    if note_clean:
                        self._history.append(CanonicalMessage(role=CanonicalMessageRole.USER, content=note_clean))
                except Exception:
                    logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)

                # Tool messages were appended to history; rebuild request and token estimates before resuming the loop.
                request = self._build_request(profile=profile, extra_tools=mcp_specs)
                estimated_input_tokens = approx_tokens_from_json(canonical_request_to_dict(request))
                context_stats = {
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimate_kind": "bytes_per_token_4",
                    "context_limit_tokens": context_limit_tokens,
                }
                if isinstance(context_limit_tokens, int) and context_limit_tokens > 0:
                    context_stats["estimated_context_left_percent"] = compute_context_left_percent(
                        used_tokens=estimated_input_tokens,
                        context_limit_tokens=context_limit_tokens,
                    )

            # Main model/tool loop.
            for _ in range(self.max_tool_turns):
                caps = profile.capabilities.with_provider_defaults(profile.provider_kind)
                use_stream = bool(self.llm_streaming and caps.supports_streaming is True)
                step_id = new_id("step")
                await self._emit(
                    kind=EventKind.LLM_REQUEST_STARTED,
                    payload={
                        "role": ModelRole.MAIN.value,
                        "context_ref": self._write_context_ref(request).to_dict(),
                        "profile_id": getattr(profile, "profile_id", None),
                        "provider_kind": profile.provider_kind.value,
                        "model": profile.model_name,
                        "timeout_s": timeout_s if timeout_s is not None else getattr(profile, "timeout_s", None),
                        "stream": use_stream,
                        "context_stats": dict(context_stats),
                        "run_mode": "llm_tools",
                    },
                    request_id=request_id,
                    turn_id=turn_id,
                    step_id=step_id,
                )

                try:
                    resp = await self._run_main_model_request_with_retry(
                        use_stream=use_stream,
                        request=request,
                        profile=profile,
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=step_id,
                        timeout_s=timeout_s,
                        cancel=cancel,
                    )
                except LLMRequestError as e:
                    await self._emit(
                        kind=EventKind.LLM_REQUEST_FAILED,
                        payload={
                            "role": ModelRole.MAIN.value,
                            "error": str(e),
                            "error_code": e.code.value,
                            "retryable": bool(e.retryable),
                            "status_code": e.status_code,
                            "provider_kind": (e.provider_kind.value if e.provider_kind is not None else None),
                            "profile_id": e.profile_id,
                            "model": e.model,
                            "request_id": e.request_id,
                            "details": dict(e.details) if isinstance(e.details, dict) else None,
                        },
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                    await self._emit(
                        kind=EventKind.OPERATION_FAILED,
                        payload={
                            "op_kind": OpKind.CHAT.value,
                            "error": str(e),
                            "error_code": e.code.value,
                            "type": "llm_request",
                        },
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=None,
                    )
                    return RunResult(status="failed", run_id=request_id, session_id=self.session_id, error=e.code.value)
                except Exception as e:
                    await self._emit(
                        kind=EventKind.LLM_REQUEST_FAILED,
                        payload={
                            "role": ModelRole.MAIN.value,
                            "error": str(e),
                            "error_code": ErrorCode.UNKNOWN.value,
                            "retryable": False,
                            "details": {"operation": "complete"},
                        },
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                    await self._emit(
                        kind=EventKind.OPERATION_FAILED,
                        payload={
                            "op_kind": OpKind.CHAT.value,
                            "error": str(e),
                            "error_code": ErrorCode.UNKNOWN.value,
                            "type": "llm_request",
                        },
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=None,
                    )
                    return RunResult(status="failed", run_id=request_id, session_id=self.session_id, error="llm_request_failed")
                tool_calls = _normalize_tool_calls(resp.tool_calls)
                normalized_resp = replace(resp, tool_calls=tool_calls)

                planned_calls: list[PlannedToolCall] = []
                if tool_calls:
                    for tc in tool_calls:
                        planned_calls.append(
                            self.tool_runtime.plan(
                                tool_execution_id=f"tool_{tc.tool_call_id}",
                                tool_name=tc.name,
                                tool_call_id=str(tc.tool_call_id),
                                arguments=dict(tc.arguments),
                            )
                        )

                await self._emit_llm_response_completed(
                    final_response=normalized_resp,
                    planned_calls=planned_calls,
                    context_stats=context_stats,
                    request_id=request_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    extra_payload={"stream": use_stream, "run_mode": "llm_tools"},
                )

                history_text = normalized_resp.text if isinstance(normalized_resp.text, str) else ""
                if not history_text.strip() and not planned_calls:
                    history_text = _EMPTY_FINAL_RESPONSE_FALLBACK_TEXT

                self._history.append(
                    CanonicalMessage(
                        role=CanonicalMessageRole.ASSISTANT,
                        content=history_text,
                        tool_calls=tool_calls or None,
                        reasoning_content=(normalized_resp.thinking if tool_calls else None),
                    )
                )

                if not planned_calls:
                    await self._emit(
                        kind=EventKind.OPERATION_COMPLETED,
                        payload={"op_kind": OpKind.CHAT.value},
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=None,
                    )
                    return RunResult(status="completed", run_id=request_id, session_id=self.session_id)

                if await self._needs_more_approval(
                    request_id=request_id,
                    turn_id=turn_id,
                    planned_calls=planned_calls,
                    decision_map={},
                    mcp_functions=mcp_functions,
                    context_stats=context_stats,
                    model_profile_id=getattr(profile, "profile_id", None),
                ):
                    snap = read_run_snapshot(project_root=self.project_root, run_id=request_id)
                    return RunResult(
                        status="needs_approval",
                        run_id=request_id,
                        session_id=self.session_id,
                        approval_id=snap.approval_id,
                        pending_tools=[
                            PendingToolCall(tool_call_id=p.tool_call_id, tool_name=p.tool_name, args=dict(p.arguments))
                            for p in planned_calls
                        ],
                    )

                for idx, planned in enumerate(planned_calls):
                    inspection = self._inspect_tool(planned=planned, mcp_functions=mcp_functions)
                    tool_message = await self._execute_planned_after_decisions(
                        planned=planned,
                        inspection=inspection,
                        decision=None,
                        request_id=request_id,
                        turn_id=turn_id,
                        mcp_functions=mcp_functions,
                    )
                    self._history.append(
                        CanonicalMessage(
                            role=CanonicalMessageRole.TOOL,
                            content=tool_message,
                            tool_call_id=planned.tool_call_id,
                            tool_name=planned.tool_name,
                        )
                    )
                    # Subagent approval passthrough: if subagent__run returns needs_approval,
                    # convert the requested internal tool calls into a first-class approval pause
                    # so UIs can prompt the user directly (CLI/web/mobile) without relying on the main agent.
                    if planned.tool_name == "subagent__run":
                        try:
                            msg = json.loads(tool_message)
                        except Exception:
                            msg = None
                        if isinstance(msg, dict):
                            sub = msg.get("result")
                            if isinstance(sub, dict):
                                report = sub.get("report")
                                if isinstance(report, str):
                                    try:
                                        report_any = json.loads(report)
                                    except Exception:
                                        report_any = None
                                    report = report_any

                                takeover_requested = False
                                status_raw = str(sub.get("status") or "").strip().lower()
                                if status_raw in {"needs_user_takeover", "user_takeover_required", "needs_takeover"}:
                                    takeover_requested = True
                                if isinstance(report, dict):
                                    report_status = str(report.get("status") or "").strip().lower()
                                    if report_status in {"needs_user_takeover", "user_takeover_required", "needs_takeover"}:
                                        takeover_requested = True
                                    if not takeover_requested:
                                        next_step = report.get("next_step_suggestion")
                                        if isinstance(next_step, dict):
                                            rec = str(next_step.get("recommended") or next_step.get("action") or "").strip().lower()
                                            if rec in {"needs_user_takeover", "user_takeover_required", "needs_takeover"}:
                                                takeover_requested = True

                                needs_raw = report.get("needs_approval") if isinstance(report, dict) else None
                                needs_list: list[dict[str, Any]] = []
                                if isinstance(needs_raw, dict):
                                    needs_list = [needs_raw]
                                elif isinstance(needs_raw, list):
                                    needs_list = [x for x in needs_raw if isinstance(x, dict)]

                                if (needs_list or takeover_requested) and self.tool_runtime is not None:
                                    pending_planned: list[PlannedToolCall] = []
                                    for req in needs_list:
                                        tool_name = req.get("tool_name")
                                        tool_call_id = req.get("tool_call_id")
                                        args_ref = req.get("arguments_ref")
                                        if not isinstance(tool_name, str) or not tool_name.strip():
                                            continue
                                        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                                            tool_call_id = new_tool_call_id()
                                        args: dict[str, Any] = {}
                                        if isinstance(args_ref, dict):
                                            try:
                                                ref = ArtifactRef.from_dict(args_ref)
                                                raw_args = self.artifact_store.get(ref)
                                                parsed = json.loads(raw_args.decode("utf-8", errors="replace"))
                                                if isinstance(parsed, dict):
                                                    args = parsed
                                            except Exception:
                                                args = {}
                                        pending_planned.append(
                                            self.tool_runtime.plan(
                                                tool_execution_id=f"tool_{tool_call_id}",
                                                tool_name=tool_name.strip(),
                                                tool_call_id=tool_call_id.strip(),
                                                arguments=args,
                                            )
                                        )

                                    if pending_planned:
                                        focus_req = needs_list[0]
                                        focus_action = str(focus_req.get("action_summary") or focus_req.get("summary") or "").strip()
                                        if focus_action:
                                            focus_action = f"Subagent requested approval: {focus_action}"
                                        else:
                                            focus_action = "Subagent requested approval"
                                        focus_risk = str(focus_req.get("risk_level") or "high").strip() or "high"
                                        focus_reason = str(focus_req.get("reason") or "Delegated task requested approval.").strip()
                                        focus_inspection = self._inspect_tool(planned=pending_planned[0], mcp_functions=mcp_functions)

                                        class _PauseInspection:
                                            action_summary: str = focus_action
                                            risk_level: str = focus_risk
                                            reason: str = focus_reason
                                            diff_ref: Any | None = getattr(focus_inspection, "diff_ref", None)

                                        approval_id = await self._pause_run(
                                            request_id=request_id,
                                            turn_id=turn_id,
                                            planned_calls=pending_planned,
                                            context_stats=context_stats,
                                            model_profile_id=getattr(profile, "profile_id", None),
                                            inspection=_PauseInspection(),
                                            focus_tool_call_id=pending_planned[0].tool_call_id,
                                            resume_payload_extra={
                                                "source": "subagent",
                                                "subagent": {
                                                    "subagent_run_id": sub.get("subagent_run_id"),
                                                    "preset": sub.get("preset"),
                                                    "origin_tool_call_id": planned.tool_call_id,
                                                    "origin_tool_execution_id": planned.tool_execution_id,
                                                    "transcript_ref": sub.get("transcript_ref"),
                                                    # Needed to continue the delegated work after the user approves.
                                                    # This is safe to persist: it's a plain JSON dict (no local paths required here).
                                                    "run_args": dict(planned.arguments) if isinstance(planned.arguments, dict) else {},
                                                    "takeover": False,
                                                },
                                            },
                                        )
                                        return RunResult(
                                            status="needs_approval",
                                            run_id=request_id,
                                            session_id=self.session_id,
                                            approval_id=approval_id,
                                            pending_tools=[
                                                PendingToolCall(tool_call_id=p.tool_call_id, tool_name=p.tool_name, args=dict(p.arguments))
                                                for p in pending_planned
                                            ],
                                            error="Subagent requested approval.",
                                        )

                                    if takeover_requested:
                                        takeover_next = report.get("next_step_suggestion") if isinstance(report, dict) else None
                                        if not isinstance(takeover_next, dict):
                                            takeover_next = {}
                                        focus_action = str(
                                            (report.get("action_summary") if isinstance(report, dict) else None)
                                            or takeover_next.get("action_summary")
                                            or "Subagent requires user takeover. Complete verification/login and approve to resume."
                                        ).strip()
                                        focus_reason = str(
                                            (report.get("reason") if isinstance(report, dict) else None)
                                            or takeover_next.get("reason")
                                            or takeover_next.get("message")
                                            or "Browser task hit CAPTCHA/login/2FA and needs human takeover."
                                        ).strip()

                                        takeover_context: dict[str, Any] = {}
                                        current_url = None
                                        if isinstance(report, dict):
                                            current_url = report.get("current_url")
                                        if not isinstance(current_url, str) or not current_url.strip():
                                            current_url = takeover_next.get("current_url")
                                        if isinstance(current_url, str) and current_url.strip():
                                            takeover_context["current_url"] = current_url.strip()

                                        screenshot = None
                                        if isinstance(report, dict):
                                            screenshot = report.get("screenshot")
                                        if not isinstance(screenshot, str) or not screenshot.strip():
                                            screenshot = takeover_next.get("screenshot")
                                        if isinstance(screenshot, str) and screenshot.strip():
                                            takeover_context["screenshot"] = screenshot.strip()

                                        next_step_hint = None
                                        if isinstance(report, dict):
                                            next_step_hint = report.get("next_step")
                                        if not isinstance(next_step_hint, str) or not next_step_hint.strip():
                                            next_step_hint = takeover_next.get("next_step")
                                        if isinstance(next_step_hint, str) and next_step_hint.strip():
                                            takeover_context["next_step"] = next_step_hint.strip()

                                        sub_run_id = sub.get("subagent_run_id") if isinstance(sub, dict) else None
                                        if isinstance(sub_run_id, str) and sub_run_id.strip():
                                            takeover_context["subagent_run_id"] = sub_run_id.strip()
                                        browser_agent_session = sub.get("browser_agent_session") if isinstance(sub, dict) else None
                                        if isinstance(browser_agent_session, str) and browser_agent_session.strip():
                                            takeover_context["browser_agent_session"] = browser_agent_session.strip()

                                        class _PauseInspection:
                                            action_summary: str = focus_action
                                            risk_level: str = "medium"
                                            reason: str = focus_reason
                                            diff_ref: Any | None = None

                                        approval_id = await self._pause_run(
                                            request_id=request_id,
                                            turn_id=turn_id,
                                            planned_calls=[],
                                            context_stats=context_stats,
                                            model_profile_id=getattr(profile, "profile_id", None),
                                            inspection=_PauseInspection(),
                                            focus_tool_call_id=None,
                                            resume_payload_extra={
                                                "source": "subagent",
                                                "subagent": {
                                                    "subagent_run_id": sub.get("subagent_run_id"),
                                                    "preset": sub.get("preset"),
                                                    "origin_tool_call_id": planned.tool_call_id,
                                                    "origin_tool_execution_id": planned.tool_execution_id,
                                                    "transcript_ref": sub.get("transcript_ref"),
                                                    "run_args": dict(planned.arguments) if isinstance(planned.arguments, dict) else {},
                                                    "takeover": True,
                                                    "takeover_context": takeover_context,
                                                },
                                            },
                                        )
                                        return RunResult(
                                            status="needs_approval",
                                            run_id=request_id,
                                            session_id=self.session_id,
                                            approval_id=approval_id,
                                            pending_tools=[],
                                            error="Subagent requested user takeover.",
                                        )

                    # DAG approval passthrough: `dag__execute_next` may dispatch subagents that request
                    # approvals for internal tool calls (e.g. shell commands) or require browser takeover.
                    # Convert those into first-class approval pauses for UI parity.
                    if planned.tool_name == "dag__execute_next":
                        try:
                            msg = json.loads(tool_message)
                        except Exception:
                            msg = None

                        approval_entries: list[tuple[str | None, dict[str, Any]]] = []
                        blocked_node: str | None = None
                        blocked_nodes: list[str] = []

                        def _is_takeover_req(req_any: dict[str, Any]) -> bool:
                            mode = str(req_any.get("mode") or req_any.get("kind") or req_any.get("status") or "").strip().lower()
                            return mode in {"user_takeover", "needs_user_takeover", "user_takeover_required", "needs_takeover"}

                        if isinstance(msg, dict):
                            dag_res = msg.get("result")
                            if isinstance(dag_res, dict):
                                blocked_node_raw = dag_res.get("blocked_node")
                                if isinstance(blocked_node_raw, str) and blocked_node_raw.strip():
                                    blocked_node = blocked_node_raw.strip()
                                    blocked_nodes.append(blocked_node)

                                blocked_nodes_raw = dag_res.get("blocked_nodes")
                                if isinstance(blocked_nodes_raw, list):
                                    for node_any in blocked_nodes_raw:
                                        if isinstance(node_any, str) and node_any.strip():
                                            blocked_nodes.append(node_any.strip())

                                node_results = dag_res.get("node_results")
                                if isinstance(node_results, dict):
                                    for node_id_any, node_any in node_results.items():
                                        if not isinstance(node_any, dict):
                                            continue
                                        if str(node_any.get("status") or "") != "needs_approval":
                                            continue
                                        node_id = node_id_any.strip() if isinstance(node_id_any, str) and node_id_any.strip() else None
                                        if node_id:
                                            blocked_nodes.append(node_id)
                                        req = node_any.get("approval_request")
                                        if isinstance(req, dict):
                                            req_copy = dict(req)
                                            if node_id and not isinstance(req_copy.get("node_id"), str):
                                                req_copy["node_id"] = node_id
                                            approval_entries.append((node_id, req_copy))

                                blocked = dag_res.get("blocked_approval")
                                if isinstance(blocked, dict):
                                    reqs = blocked.get("requests")
                                    if isinstance(reqs, list):
                                        for req_any in reqs:
                                            if not isinstance(req_any, dict):
                                                continue
                                            req_copy = dict(req_any)
                                            if blocked_node and not isinstance(req_copy.get("node_id"), str):
                                                req_copy["node_id"] = blocked_node
                                            approval_entries.append((blocked_node, req_copy))
                                    else:
                                        req_copy = dict(blocked)
                                        if blocked_node and not isinstance(req_copy.get("node_id"), str):
                                            req_copy["node_id"] = blocked_node
                                        approval_entries.append((blocked_node, req_copy))
                                elif isinstance(blocked, list):
                                    for req_any in blocked:
                                        if not isinstance(req_any, dict):
                                            continue
                                        req_copy = dict(req_any)
                                        if blocked_node and not isinstance(req_copy.get("node_id"), str):
                                            req_copy["node_id"] = blocked_node
                                        approval_entries.append((blocked_node, req_copy))

                                blocked_many = dag_res.get("blocked_approvals")
                                if isinstance(blocked_many, list):
                                    for item in blocked_many:
                                        if not isinstance(item, dict):
                                            continue
                                        node_id_any = item.get("node_id")
                                        node_id = node_id_any.strip() if isinstance(node_id_any, str) and node_id_any.strip() else None
                                        if node_id:
                                            blocked_nodes.append(node_id)
                                        req_copy = dict(item)
                                        if node_id and not isinstance(req_copy.get("node_id"), str):
                                            req_copy["node_id"] = node_id
                                        approval_entries.append((node_id, req_copy))

                        # De-duplicate requests while preserving order.
                        dedup: set[tuple[str, str, str, str, str, str]] = set()
                        deduped_entries: list[tuple[str | None, dict[str, Any]]] = []
                        for node_id, req in approval_entries:
                            if not isinstance(req, dict):
                                continue
                            req_node = req.get("node_id")
                            node_val = req_node.strip() if isinstance(req_node, str) and req_node.strip() else (node_id or "")
                            mode = str(req.get("mode") or req.get("kind") or req.get("status") or "").strip().lower()
                            tool_name = str(req.get("tool_name") or req.get("tool") or "").strip()
                            tool_call_id = str(req.get("tool_call_id") or "").strip()
                            current_url = str(req.get("current_url") or "").strip()
                            action = str(req.get("action_summary") or req.get("summary") or req.get("reason") or "").strip()
                            key = (node_val, mode, tool_name, tool_call_id, current_url, action)
                            if key in dedup:
                                continue
                            dedup.add(key)
                            deduped_entries.append((node_val or node_id, req))

                        approval_entries = deduped_entries

                        if approval_entries and self.tool_runtime is not None:
                            # Release all blocked DAG nodes so they can be redispatched after each decision.
                            if self._dag_runner is not None:
                                release_nodes: set[str] = set()
                                if blocked_node:
                                    release_nodes.add(blocked_node)
                                for node_id in blocked_nodes:
                                    if isinstance(node_id, str) and node_id.strip():
                                        release_nodes.add(node_id.strip())
                                for node_id, req in approval_entries:
                                    if isinstance(node_id, str) and node_id.strip():
                                        release_nodes.add(node_id.strip())
                                    req_node = req.get("node_id") if isinstance(req, dict) else None
                                    if isinstance(req_node, str) and req_node.strip():
                                        release_nodes.add(req_node.strip())
                                for node_id in sorted(release_nodes):
                                    try:
                                        self._dag_runner.release_running(node_id)
                                    except Exception:
                                        logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)

                            # Serialize concurrent approvals/takeovers: focus one request at a time.
                            focus_idx = 0
                            for i_entry, (_node_id, req) in enumerate(approval_entries):
                                if isinstance(req, dict) and _is_takeover_req(req):
                                    focus_idx = i_entry
                                    break

                            focus_node, focus_req = approval_entries[focus_idx]
                            queue_summary: list[dict[str, Any]] = []
                            for i_entry, (node_id, req) in enumerate(approval_entries):
                                if i_entry == focus_idx or not isinstance(req, dict):
                                    continue
                                queue_summary.append(
                                    {
                                        "node_id": node_id,
                                        "mode": str(req.get("mode") or req.get("kind") or req.get("status") or "").strip().lower() or None,
                                        "tool_name": str(req.get("tool_name") or req.get("tool") or "").strip() or None,
                                        "action_summary": str(req.get("action_summary") or req.get("summary") or "").strip() or None,
                                        "current_url": str(req.get("current_url") or "").strip() or None,
                                        "subagent_run_id": str(req.get("subagent_run_id") or "").strip() or None,
                                        "browser_agent_session": str(req.get("browser_agent_session") or req.get("agent_session") or "").strip() or None,
                                    }
                                )

                            if _is_takeover_req(focus_req):
                                base_action = str(focus_req.get("action_summary") or focus_req.get("summary") or "").strip()
                                if focus_node:
                                    prefix = f"DAG node {focus_node} requires user takeover"
                                else:
                                    prefix = "DAG requires user takeover"
                                focus_action = f"{prefix}: {base_action}" if base_action else f"{prefix}. Complete verification/login and approve to resume."
                                focus_reason = str(
                                    focus_req.get("reason")
                                    or "Browser task hit CAPTCHA/login/2FA and needs human takeover."
                                ).strip()

                                takeover_context: dict[str, Any] = {}
                                current_url = focus_req.get("current_url")
                                if isinstance(current_url, str) and current_url.strip():
                                    takeover_context["current_url"] = current_url.strip()
                                screenshot = focus_req.get("screenshot")
                                if isinstance(screenshot, str) and screenshot.strip():
                                    takeover_context["screenshot"] = screenshot.strip()
                                next_step_hint = focus_req.get("next_step")
                                if not isinstance(next_step_hint, str) or not next_step_hint.strip():
                                    nss = focus_req.get("next_step_suggestion")
                                    if isinstance(nss, dict):
                                        next_step_hint = nss.get("next_step") or nss.get("message")
                                if isinstance(next_step_hint, str) and next_step_hint.strip():
                                    takeover_context["next_step"] = next_step_hint.strip()
                                req_sub_run_id = focus_req.get("subagent_run_id")
                                if isinstance(req_sub_run_id, str) and req_sub_run_id.strip():
                                    takeover_context["subagent_run_id"] = req_sub_run_id.strip()
                                req_browser_session = focus_req.get("browser_agent_session")
                                if not isinstance(req_browser_session, str) or not req_browser_session.strip():
                                    req_browser_session = focus_req.get("agent_session")
                                if isinstance(req_browser_session, str) and req_browser_session.strip():
                                    takeover_context["browser_agent_session"] = req_browser_session.strip()

                                class _PauseInspection:
                                    action_summary: str = focus_action
                                    risk_level: str = "medium"
                                    reason: str = focus_reason
                                    diff_ref: Any | None = None

                                approval_id = await self._pause_run(
                                    request_id=request_id,
                                    turn_id=turn_id,
                                    planned_calls=[],
                                    context_stats=context_stats,
                                    model_profile_id=getattr(profile, "profile_id", None),
                                    inspection=_PauseInspection(),
                                    focus_tool_call_id=None,
                                    resume_payload_extra={
                                        "source": "dag",
                                        "dag": {
                                            "blocked_node": focus_node,
                                            "origin_tool_call_id": planned.tool_call_id,
                                            "origin_tool_execution_id": planned.tool_execution_id,
                                            "dag_execute_args": dict(planned.arguments) if isinstance(planned.arguments, dict) else {},
                                            "takeover": True,
                                            "takeover_context": takeover_context,
                                            "pending_queue": queue_summary,
                                        },
                                    },
                                )
                                return RunResult(
                                    status="needs_approval",
                                    run_id=request_id,
                                    session_id=self.session_id,
                                    approval_id=approval_id,
                                    pending_tools=[],
                                    error="DAG requested user takeover.",
                                )

                            focus_reqs_raw = focus_req.get("requests") if isinstance(focus_req, dict) else None
                            focus_reqs: list[dict[str, Any]] = []
                            if isinstance(focus_reqs_raw, list):
                                focus_reqs = [r for r in focus_reqs_raw if isinstance(r, dict)]
                            if not focus_reqs:
                                focus_reqs = [focus_req]

                            pending_planned: list[PlannedToolCall] = []
                            for req in focus_reqs:
                                if not isinstance(req, dict):
                                    continue
                                tool_name = req.get("tool_name")
                                if not isinstance(tool_name, str) or not tool_name.strip():
                                    tool_name = req.get("tool")
                                if not isinstance(tool_name, str) or not tool_name.strip():
                                    continue
                                tool_call_id = req.get("tool_call_id")
                                if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                                    tool_call_id = new_tool_call_id()

                                args: dict[str, Any] = {}
                                args_ref = req.get("arguments_ref")
                                if isinstance(args_ref, dict):
                                    try:
                                        ref = ArtifactRef.from_dict(args_ref)
                                        raw_args = self.artifact_store.get(ref)
                                        parsed = json.loads(raw_args.decode("utf-8", errors="replace"))
                                        if isinstance(parsed, dict):
                                            args = parsed
                                    except Exception:
                                        args = {}
                                elif isinstance(req.get("arguments"), dict):
                                    args = dict(req["arguments"])

                                pending_planned.append(
                                    self.tool_runtime.plan(
                                        tool_execution_id=f"tool_{tool_call_id}",
                                        tool_name=tool_name.strip(),
                                        tool_call_id=tool_call_id.strip(),
                                        arguments=args,
                                    )
                                )

                            if pending_planned:
                                focus_action = str(focus_req.get("action_summary") or focus_req.get("summary") or "").strip()
                                if focus_node:
                                    prefix = f"DAG node {focus_node} requested approval"
                                else:
                                    prefix = "DAG requested approval"
                                if focus_action:
                                    focus_action = f"{prefix}: {focus_action}"
                                else:
                                    focus_action = prefix
                                focus_risk = str(focus_req.get("risk_level") or "high").strip() or "high"
                                focus_reason = str(focus_req.get("reason") or "DAG-dispatched subagent requested approval.").strip()
                                focus_inspection = self._inspect_tool(planned=pending_planned[0], mcp_functions=mcp_functions)

                                class _PauseInspection:
                                    action_summary: str = focus_action
                                    risk_level: str = focus_risk
                                    reason: str = focus_reason
                                    diff_ref: Any | None = getattr(focus_inspection, "diff_ref", None)

                                approval_id = await self._pause_run(
                                    request_id=request_id,
                                    turn_id=turn_id,
                                    planned_calls=pending_planned,
                                    context_stats=context_stats,
                                    model_profile_id=getattr(profile, "profile_id", None),
                                    inspection=_PauseInspection(),
                                    focus_tool_call_id=pending_planned[0].tool_call_id,
                                    resume_payload_extra={
                                        "source": "dag",
                                        "dag": {
                                            "blocked_node": focus_node,
                                            "origin_tool_call_id": planned.tool_call_id,
                                            "origin_tool_execution_id": planned.tool_execution_id,
                                            "dag_execute_args": dict(planned.arguments) if isinstance(planned.arguments, dict) else {},
                                            "takeover": False,
                                            "pending_queue": queue_summary,
                                        },
                                    },
                                )
                                return RunResult(
                                    status="needs_approval",
                                    run_id=request_id,
                                    session_id=self.session_id,
                                    approval_id=approval_id,
                                    pending_tools=[
                                        PendingToolCall(tool_call_id=p.tool_call_id, tool_name=p.tool_name, args=dict(p.arguments))
                                        for p in pending_planned
                                    ],
                                    error="DAG requested approval.",
                                )
                    # Fail-fast: stop executing further tool calls from the same assistant message after a failure.
                    # Close remaining tool calls as cancelled to keep tool-call pairing valid across providers.
                    try:
                        msg = json.loads(tool_message)
                    except Exception:
                        msg = None
                    if isinstance(msg, dict) and msg.get("ok") is False:
                        prev_tool = planned.tool_name
                        prev_code = msg.get("error_code") if isinstance(msg.get("error_code"), str) else None
                        prev_err = msg.get("error") if isinstance(msg.get("error"), str) else None
                        reason = f"Skipped because a previous tool call failed: {prev_tool}"
                        if prev_code:
                            reason = f"{reason} ({prev_code})"
                        if prev_err:
                            reason = f"{reason}: {prev_err}"
                        if len(reason) > 400:
                            reason = reason[:399].rstrip() + "…"

                        for remaining in planned_calls[idx + 1 :]:
                            skipped_message = await self._tool_result_error(
                                planned=remaining,
                                request_id=request_id,
                                turn_id=turn_id,
                                error_code=ErrorCode.CANCELLED.value,
                                error_message=reason,
                                status="cancelled",
                            )
                            self._history.append(
                                CanonicalMessage(
                                    role=CanonicalMessageRole.TOOL,
                                    content=skipped_message,
                                    tool_call_id=remaining.tool_call_id,
                                    tool_name=remaining.tool_name,
                                )
                            )
                        break

                request = self._build_request(profile=profile, extra_tools=mcp_specs)

            await self._emit(
                kind=EventKind.OPERATION_FAILED,
                payload={"error": "Exceeded tool loop limit.", "error_code": ErrorCode.TOOL_LOOP_LIMIT.value},
                request_id=request_id,
                turn_id=turn_id,
                step_id=None,
            )
            return RunResult(status="failed", run_id=request_id, session_id=self.session_id, error="tool_loop_limit")

    async def _perform_compaction(
        self,
        *,
        trigger: str,
        request_id: str,
        turn_id: str | None,
        timeout_s: float | None,
        cancel: CancellationToken | None,
        context_stats: dict[str, Any] | None = None,
        threshold_ratio: float | None = None,
        extra_tools: list[ToolSpec] | None = None,
    ) -> bool:
        cancel = cancel or CancellationToken()
        if cancel.cancelled:
            await self._emit(
                kind=EventKind.OPERATION_CANCELLED,
                payload={
                    "op_kind": OpKind.COMPACT.value,
                    "error_code": ErrorCode.CANCELLED.value,
                    "reason": "cancelled",
                    "phase": "compact",
                },
                request_id=request_id,
                turn_id=turn_id,
                step_id=None,
            )
            return False

        if self._history is None:
            self._history = []

        is_auto = trigger == "auto"
        has_summary = isinstance(self.memory_summary, str) and self.memory_summary.strip()
        if not self._history and not has_summary:
            await self._emit(
                kind=EventKind.OPERATION_FAILED,
                payload={
                    "op_kind": OpKind.COMPACT.value,
                    "error": "Nothing to compact (empty history).",
                    "error_code": ErrorCode.BAD_REQUEST.value,
                    "type": "compact_empty",
                },
                request_id=request_id,
                turn_id=turn_id,
                step_id=None,
            )
            return False

        before_count = len(self._history)
        step_id = new_id("step")
        await self._emit(
            kind=EventKind.OPERATION_STARTED,
            payload={
                "op_kind": OpKind.COMPACT.value,
                "trigger": trigger,
                "context_stats": dict(context_stats or {}),
                "threshold_ratio": threshold_ratio,
            },
            request_id=request_id,
            turn_id=turn_id,
            step_id=step_id,
        )

        profile = self.model_config.get_profile_for_role(ModelRole.EXTRACT) or self.model_config.get_profile_for_role(ModelRole.MAIN)
        if profile is None:
            await self._emit(
                kind=EventKind.OPERATION_FAILED,
                payload={
                    "op_kind": OpKind.COMPACT.value,
                    "error": "No model profile configured for compaction.",
                    "error_code": ErrorCode.MODEL_RESOLUTION.value,
                    "type": "compact_model_missing",
                },
                request_id=request_id,
                turn_id=turn_id,
                step_id=step_id,
            )
            return False

        try:
            from .compaction import apply_compaction_retention, build_compaction_request, load_compact_prompt_text, settings_for_profile
        except Exception as e:
            await self._emit(
                kind=EventKind.OPERATION_FAILED,
                payload={
                    "op_kind": OpKind.COMPACT.value,
                    "error": f"Compaction module unavailable: {e}",
                    "error_code": ErrorCode.UNKNOWN.value,
                    "type": "compact_import",
                },
                request_id=request_id,
                turn_id=turn_id,
                step_id=step_id,
            )
            return False

        cm = settings_for_profile(profile)
        context_limit_tokens = resolve_context_limit_tokens(profile.limits.context_limit_tokens if profile.limits is not None else None)

        compact_prompt = load_compact_prompt_text()
        compact_request = build_compaction_request(
            history=list(self._history),
            memory_summary=self.memory_summary,
            prompt_text=compact_prompt,
            tool_output_budget_tokens=cm.tool_output_budget_tokens,
        )

        await self._emit(
            kind=EventKind.LLM_REQUEST_STARTED,
            payload={
                "role": ModelRole.EXTRACT.value,
                "context_ref": self._write_context_ref(compact_request).to_dict(),
                "profile_id": getattr(profile, "profile_id", None),
                "provider_kind": profile.provider_kind.value,
                "model": profile.model_name,
                "timeout_s": timeout_s if timeout_s is not None else getattr(profile, "timeout_s", None),
                "stream": False,
                "run_mode": "llm_compact",
            },
            request_id=request_id,
            turn_id=turn_id,
            step_id=step_id,
        )

        try:
            # Compaction is a plain text completion (no tools).
            resp = await self._run_agent_once(
                request=compact_request,
                profile=profile,
                request_id=request_id,
                turn_id=turn_id,
                timeout_s=timeout_s,
                cancel=cancel,
            )
        except Exception as e:
            await self._emit(
                kind=EventKind.LLM_REQUEST_FAILED,
                payload={
                    "role": ModelRole.EXTRACT.value,
                    "error": str(e),
                    "error_code": ErrorCode.UNKNOWN.value,
                },
                request_id=request_id,
                turn_id=turn_id,
                step_id=step_id,
            )
            await self._emit(
                kind=EventKind.OPERATION_FAILED,
                payload={
                    "op_kind": OpKind.COMPACT.value,
                    "error": str(e),
                    "error_code": ErrorCode.UNKNOWN.value,
                    "type": "compact_llm",
                },
                request_id=request_id,
                turn_id=turn_id,
                step_id=step_id,
            )
            return False

        raw_summary = str(resp.text or "").strip()
        if not raw_summary:
            raw_summary = "(empty summary)"

        raw_summary_ref = self.artifact_store.put(
            raw_summary,
            kind="compact_raw_summary",
            meta={"summary": "Compaction raw summary"},
        )

        retained = apply_compaction_retention(
            history=list(self._history),
            memory_summary=raw_summary,
            context_limit_tokens=context_limit_tokens,
            history_budget_ratio=cm.history_budget_ratio,
            history_budget_fallback_tokens=cm.history_budget_fallback_tokens,
        )

        self.memory_summary = retained.memory_summary
        self._history = list(retained.retained_history)
        after_count = len(self._history)

        summary_ref = self.artifact_store.put(
            retained.memory_summary,
            kind="compact_summary",
            meta={"summary": "Compaction durable summary"},
        )

        snap = self.snapshot_backend.snapshot_create(reason=f"compaction:{trigger}")
        snapshot_ref = self.artifact_store.put(
            json.dumps({"commit": snap.commit, "label": snap.label}, ensure_ascii=False, sort_keys=True, indent=2),
            kind="compact_snapshot",
            meta={"summary": "Compaction snapshot"},
        )

        try:
            patch: dict[str, Any] = {
                "memory_summary": retained.memory_summary,
                "last_compacted_at": now_ts_ms(),
                "last_compaction_trigger": trigger,
                "last_compaction_summary_ref": summary_ref.to_dict(),
            }
            if isinstance(context_stats, dict):
                patch["last_compaction_context_stats"] = dict(context_stats)
            self.session_store.update_session(self.session_id, patch)
        except Exception:
            logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)

        post_request = self._build_request(profile=profile, extra_tools=extra_tools)
        post_estimated_input_tokens = approx_tokens_from_json(canonical_request_to_dict(post_request))
        post_stats: dict[str, Any] = {
            "estimated_input_tokens": post_estimated_input_tokens,
            "estimate_kind": "bytes_per_token_4",
            "context_limit_tokens": context_limit_tokens,
        }
        if isinstance(context_limit_tokens, int) and context_limit_tokens > 0:
            post_stats["estimated_context_left_percent"] = compute_context_left_percent(
                used_tokens=post_estimated_input_tokens,
                context_limit_tokens=context_limit_tokens,
            )

        await self._emit(
            kind=EventKind.OPERATION_COMPLETED,
            payload={
                "op_kind": OpKind.COMPACT.value,
                "trigger": trigger,
                "raw_summary_ref": raw_summary_ref.to_dict(),
                "summary_ref": summary_ref.to_dict(),
                "snapshot_ref": snapshot_ref.to_dict(),
                "history_before_count": before_count,
                "history_after_count": after_count,
                "history_budget_tokens": retained.history_budget_tokens,
                "summary_estimated_tokens": retained.summary_estimated_tokens,
                "context_stats": post_stats,
                "auto": bool(is_auto),
            },
            request_id=request_id,
            turn_id=turn_id,
            step_id=step_id,
        )

        return True

    def _llm_retry_delay_s(self, *, attempt: int) -> float:
        if attempt <= 0:
            return 0.0
        delay = self._llm_network_retry_base_delay_s * float(2 ** max(0, attempt - 1))
        return max(0.0, min(self._llm_network_retry_max_delay_s, delay))

    def _should_retry_llm_request_error(self, *, error: LLMRequestError, attempt: int) -> bool:
        if attempt >= self._llm_network_retry_attempts:
            return False
        if not bool(error.retryable):
            return False
        if error.code not in {
            ErrorCode.NETWORK_ERROR,
            ErrorCode.TIMEOUT,
            ErrorCode.SERVER_ERROR,
            ErrorCode.RATE_LIMIT,
        }:
            return False
        details = error.details if isinstance(error.details, dict) else {}
        # Streaming retries after deltas are unsafe (UI may show duplicated partial output).
        if details.get("had_output") is True:
            return False
        return True

    async def _run_main_model_request_with_retry(
        self,
        *,
        use_stream: bool,
        request: CanonicalRequest,
        profile: ModelProfile,
        request_id: str,
        turn_id: str | None,
        step_id: str | None,
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> LLMResponse:
        attempt = 1
        while True:
            try:
                if use_stream:
                    return await self._run_agent_stream(
                        request=request,
                        profile=profile,
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=step_id,
                        timeout_s=timeout_s,
                        cancel=cancel,
                    )
                return await self._run_agent_once(
                    request=request,
                    profile=profile,
                    request_id=request_id,
                    turn_id=turn_id,
                    timeout_s=timeout_s,
                    cancel=cancel,
                )
            except LLMRequestError as e:
                if not self._should_retry_llm_request_error(error=e, attempt=attempt):
                    raise
                delay_s = self._llm_retry_delay_s(attempt=attempt)
                logger.warning(
                    "Retrying model request after transient failure: "
                    "attempt=%s/%s code=%s provider=%s model=%s delay_s=%.2f error=%s",
                    attempt,
                    self._llm_network_retry_attempts,
                    e.code.value,
                    profile.provider_kind.value,
                    profile.model_name,
                    delay_s,
                    str(e),
                )
                if cancel is not None and cancel.cancelled:
                    raise
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
                if cancel is not None and cancel.cancelled:
                    raise
                attempt += 1

    async def _run_agent_once(
        self,
        *,
        request: CanonicalRequest,
        profile: ModelProfile,
        request_id: str,
        turn_id: str | None,
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> LLMResponse:
        """
        Execute a single model request via Aura provider adapters (no agno.Agent).

        The main engine owns canonical history, tool orchestration, approvals, and persistence.
        Subagents remain agno.Agent-backed for isolation (see `aura/runtime/subagents/runner.py`).
        """
        trace = LLMTrace.maybe_create(
            project_root=self.project_root,
            session_id=self.session_id,
            request_id=request_id,
            turn_id=turn_id,
            step_id=None,
        )
        if trace is not None:
            trace.record_canonical_request(request)

        def _complete_sync() -> LLMResponse:
            kind = profile.provider_kind
            if kind is ProviderKind.OPENAI_COMPATIBLE:
                return complete_openai_compatible(
                    profile=profile,
                    request=request,
                    timeout_s=timeout_s,
                    cancel=cancel,
                    trace=trace,
                )
            if kind is ProviderKind.OPENAI_CODEX:
                return complete_openai_codex(
                    profile=profile,
                    request=request,
                    timeout_s=timeout_s,
                    cancel=cancel,
                    trace=trace,
                )
            if kind is ProviderKind.ANTHROPIC:
                return complete_anthropic(
                    profile=profile,
                    request=request,
                    timeout_s=timeout_s,
                    cancel=cancel,
                    trace=trace,
                )
            if kind is ProviderKind.GEMINI:
                return complete_gemini(
                    profile=profile,
                    request=request,
                    timeout_s=timeout_s,
                    cancel=cancel,
                    trace=trace,
                )
            raise RuntimeError(f"Unsupported provider_kind: {kind}")

        try:
            resp = await asyncio.to_thread(_complete_sync)
        except LLMRequestError:
            raise
        except Exception as e:
            raise wrap_provider_exception(
                e,
                provider_kind=profile.provider_kind,
                profile_id=profile.profile_id,
                model=profile.model_name,
                operation="complete",
            ) from e
        if not resp.model:
            resp = replace(resp, model=profile.model_name)
        return resp

    async def _run_agent_stream(
        self,
        *,
        request: CanonicalRequest,
        profile: ModelProfile,
        request_id: str,
        turn_id: str | None,
        step_id: str | None,
        timeout_s: float | None,
        cancel: CancellationToken | None,
    ) -> LLMResponse:
        """
        Stream a single model request and forward deltas onto the EventBus.

        Emits `llm_thinking_delta` and `llm_response_delta` events for live UI rendering, while
        returning the final `LLMResponse` for normal tool-loop processing.
        """
        trace = LLMTrace.maybe_create(
            project_root=self.project_root,
            session_id=self.session_id,
            request_id=request_id,
            turn_id=turn_id,
            step_id=None,
        )
        if trace is not None:
            trace.record_canonical_request(request)

        def _stream_sync() -> Any:
            kind = profile.provider_kind
            if kind is ProviderKind.OPENAI_COMPATIBLE:
                return stream_openai_compatible(profile=profile, request=request, timeout_s=timeout_s, cancel=cancel, trace=trace)
            if kind is ProviderKind.OPENAI_CODEX:
                return stream_openai_codex(profile=profile, request=request, timeout_s=timeout_s, cancel=cancel, trace=trace)
            if kind is ProviderKind.ANTHROPIC:
                return stream_anthropic(profile=profile, request=request, timeout_s=timeout_s, cancel=cancel, trace=trace)
            if kind is ProviderKind.GEMINI:
                return stream_gemini(profile=profile, request=request, timeout_s=timeout_s, cancel=cancel, trace=trace)
            raise RuntimeError(f"Unsupported provider_kind: {kind}")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue[LLMStreamEvent | BaseException | None] = asyncio.Queue()

        def _producer() -> None:
            try:
                stream_iter = _stream_sync()
                for ev in stream_iter:
                    loop.call_soon_threadsafe(q.put_nowait, ev)
                loop.call_soon_threadsafe(q.put_nowait, None)
            except BaseException as e:
                loop.call_soon_threadsafe(q.put_nowait, e)

        threading.Thread(target=_producer, name="aura-llm-stream", daemon=True).start()

        final: LLMResponse | None = None
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        streamed_tool_calls: list[ToolCall] = []
        had_stream_output = False
        while True:
            item = await q.get()
            if item is None:
                break
            if isinstance(item, BaseException):
                if isinstance(item, LLMRequestError):
                    if had_stream_output:
                        details = dict(item.details) if isinstance(item.details, dict) else {}
                        details["had_output"] = True
                        item.details = details
                    raise item
                wrapped = wrap_provider_exception(
                    item,
                    provider_kind=profile.provider_kind,
                    profile_id=profile.profile_id,
                    model=profile.model_name,
                    operation="stream",
                )
                if had_stream_output:
                    details = dict(wrapped.details) if isinstance(wrapped.details, dict) else {}
                    details["had_output"] = True
                    wrapped.details = details
                raise wrapped from item
            ev = item
            if ev.kind == LLMStreamEventKind.THINKING_DELTA:
                if ev.thinking_delta:
                    had_stream_output = True
                    thinking_parts.append(ev.thinking_delta)
                    await self._emit(
                        kind=EventKind.LLM_THINKING_DELTA,
                        payload={"thinking_delta": ev.thinking_delta},
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=step_id,
                    )
            elif ev.kind == LLMStreamEventKind.TEXT_DELTA:
                if ev.text_delta:
                    had_stream_output = True
                    text_parts.append(ev.text_delta)
                    await self._emit(
                        kind=EventKind.LLM_RESPONSE_DELTA,
                        payload={"text_delta": ev.text_delta},
                        request_id=request_id,
                        turn_id=turn_id,
                        step_id=step_id,
                    )
            elif ev.kind == LLMStreamEventKind.TOOL_CALL:
                # Some providers (or gateways) stream tool calls but omit them from the final `response.completed`.
                # Preserve them so the normal tool-loop can proceed after streaming finishes.
                if ev.tool_call is not None:
                    had_stream_output = True
                    streamed_tool_calls.append(ev.tool_call)
            elif ev.kind == LLMStreamEventKind.COMPLETED:
                if ev.response is not None:
                    final = ev.response

        if final is None:
            raise RuntimeError("Stream ended without a terminal response.")
        if not final.text and text_parts:
            final = replace(final, text="".join(text_parts))
        if final.thinking is None and thinking_parts:
            final = replace(final, thinking="".join(thinking_parts))
        if streamed_tool_calls and not final.tool_calls:
            # Dedupe by (tool_call_id, name, raw_arguments).
            seen: set[tuple[str | None, str, str | None]] = set()
            merged: list[ToolCall] = []
            for tc in streamed_tool_calls:
                key = (tc.tool_call_id, tc.name, tc.raw_arguments)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(tc)
            final = replace(final, tool_calls=merged)
        if not final.model:
            final = replace(final, model=profile.model_name)
        if trace is not None:
            try:
                trace.record_response(final)
            except Exception:
                logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)
        return final

    def _adapt_tool_specs_for_profile(self, *, tools: list[ToolSpec], profile: ModelProfile) -> list[ToolSpec]:
        """
        Some providers validate tool parameter schemas strictly and only support a subset of JSON Schema.

        Example: Gemini gateways may reject `oneOf`/`anyOf`/`const` in tool parameter schemas with a 400,
        even before the model generates output. Aura keeps strict schemas for local/runtime validation, but
        adapts the *declared* tool schemas for compatibility at request time.
        """

        from .llm.tool_schema_compat import adapt_tool_specs_for_profile

        return adapt_tool_specs_for_profile(tools=tools, profile=profile)

    def _build_request(self, *, profile: ModelProfile | None = None, extra_tools: list[ToolSpec] | None = None) -> CanonicalRequest:
        tools: list[ToolSpec] = []
        if self.tools_enabled and self.tool_registry is not None:
            tools = [t for t in self.tool_registry.list_specs() if t.name in _DEFAULT_EXPOSED_TOOL_NAMES]
        if extra_tools:
            tools = [*tools, *list(extra_tools)]
        if profile is not None and tools:
            try:
                tools = self._adapt_tool_specs_for_profile(tools=tools, profile=profile)
            except Exception:
                logger.warning("Suppressed exception in _build_request.", exc_info=True)
        skills = self.skill_store.list()
        dag_plan = self.plan_store.get().plan
        todo = self.todo_store.get().todo

        state = self.spec_state_store.get()
        spec_summary = SpecStatusSummary(status=state.status, label=state.label)

        surface = build_agent_surface(tools=tools, skills=skills, dag_plan=dag_plan, todo=todo, spec=spec_summary)

        base_system = self.system_prompt or _load_default_system_prompt()
        parts = [base_system]
        if isinstance(self.memory_summary, str) and self.memory_summary.strip():
            parts.append("Session memory summary:\n\n" + self.memory_summary.strip())
        parts.append(surface)
        system = render_prompt_template("\n\n".join(parts))
        return CanonicalRequest(system=system, messages=list(self._history or []), tools=tools)

    async def _load_mcp_tooling(self, *, stack: AsyncExitStack) -> tuple[dict[str, Any], list[ToolSpec]]:
        """
        Build MCPTools instances from `.aura/config/mcp.json`, enter them, and return:
        - mapping: tool_name -> agno Function (async)
        - specs: ToolSpec list for agent surface
        """
        cfg = load_mcp_config(project_root=self.project_root)
        if not cfg.servers:
            return {}, []

        functions: dict[str, Any] = {}
        specs: list[ToolSpec] = []

        try:
            from agno.tools.mcp.mcp import MCPTools
            from mcp import StdioServerParameters
            from mcp.client.stdio import get_default_environment
        except Exception:
            return {}, []

        def _prefix_for(server_name: str) -> str:
            # Ensure stable + short-ish prefix, avoid exceeding provider tool name limits.
            normalized = "".join(ch if (ch.isalnum() or ch in {"_", "-"}) else "_" for ch in server_name.strip())
            normalized = normalized.strip("_") or "server"
            digest = sha256(server_name.encode("utf-8", errors="ignore")).hexdigest()[:6]
            base = normalized[:12]
            return f"mcp__{base}_{digest}__"

        for name, server in sorted(cfg.servers.items()):
            if not server.enabled:
                continue
            if not server.command:
                continue
            env = {**get_default_environment(), **dict(server.env or {})}
            server_params = StdioServerParameters(
                command=server.command,
                args=list(server.args),
                env=env,
                cwd=server.cwd,
            )
            toolkit = MCPTools(
                server_params=server_params,
                transport="stdio",
                timeout_seconds=int(max(1, server.timeout_s)),
                tool_name_prefix=_prefix_for(name),
            )
            entered = await stack.enter_async_context(toolkit)
            try:
                async_functions = entered.get_async_functions()
            except Exception:
                continue
            for tool_name, fn in async_functions.items():
                functions[tool_name] = fn
                specs.append(
                    ToolSpec(
                        name=str(getattr(fn, "name", tool_name)),
                        description=str(getattr(fn, "description", "") or ""),
                        input_schema=dict(getattr(fn, "parameters", {}) or {"type": "object", "properties": {}}),
                    )
                )

        return functions, specs

    def _inspect_tool(self, *, planned: PlannedToolCall, mcp_functions: dict[str, Any]):
        """
        Aura tools use ToolRuntime.inspect(). MCP tools default to approval unless trusted.
        """
        if self.tool_runtime is None:
            raise RuntimeError("Tool runtime not initialized.")
        if self.tool_runtime.get_tool(planned.tool_name) is not None:
            return self.tool_runtime.inspect(planned)
        if planned.tool_name in mcp_functions:
            from .tools.runtime import InspectionResult, ToolApprovalMode

            mode = self.tool_runtime.get_approval_mode()
            if mode is ToolApprovalMode.TRUSTED:
                return InspectionResult(
                    decision=InspectionDecision.ALLOW,
                    action_summary=f"Execute MCP tool: {planned.tool_name}",
                    risk_level="high",
                    reason="Approval mode is trusted (auto-allow).",
                    error_code=None,
                    diff_ref=None,
                )
            diff_ref = self.tool_runtime.artifact_store.put(
                json.dumps(planned.arguments, ensure_ascii=False, sort_keys=True, indent=2),
                kind="diff",
                meta={"summary": f"Preview for {planned.tool_name}"},
            )
            return InspectionResult(
                decision=InspectionDecision.REQUIRE_APPROVAL,
                action_summary=f"Execute MCP tool: {planned.tool_name}",
                risk_level="high",
                reason="MCP tools are treated as high-risk by default.",
                error_code=None,
                diff_ref=diff_ref,
            )
        return self.tool_runtime.inspect(planned)

    async def _needs_more_approval(
        self,
        *,
        request_id: str,
        turn_id: str | None,
        planned_calls: list[PlannedToolCall],
        decision_map: dict[str, ToolDecision],
        mcp_functions: dict[str, Any],
        context_stats: dict[str, Any],
        model_profile_id: str | None,
    ) -> bool:
        for planned in planned_calls:
            inspection = self._inspect_tool(planned=planned, mcp_functions=mcp_functions)
            if inspection.decision is InspectionDecision.REQUIRE_APPROVAL and planned.tool_call_id not in decision_map:
                await self._pause_run(
                    request_id=request_id,
                    turn_id=turn_id,
                    planned_calls=planned_calls,
                    context_stats=context_stats,
                    model_profile_id=model_profile_id,
                    inspection=inspection,
                    focus_tool_call_id=planned.tool_call_id,
                )
                return True
        return False

    async def _execute_planned_after_decisions(
        self,
        *,
        planned: PlannedToolCall,
        inspection: Any,
        decision: ToolDecision | None,
        request_id: str,
        turn_id: str | None,
        mcp_functions: dict[str, Any],
    ) -> str:
        if inspection.decision is InspectionDecision.DENY:
            return await self._tool_result_denied(planned=planned, inspection=inspection, request_id=request_id, turn_id=turn_id)
        # Respect explicit denial decisions even if the tool would otherwise be allowed.
        # This is important for approvals that originate outside the main LLM tool gating
        # (e.g. delegated/subagent approvals surfaced to the user).
        if decision is not None and decision.decision != "approve":
            return await self._tool_result_denied_by_user(
                planned=planned,
                request_id=request_id,
                turn_id=turn_id,
                note=decision.note,
            )
        if inspection.decision is InspectionDecision.REQUIRE_APPROVAL:
            if decision is None or decision.decision != "approve":
                return await self._tool_result_denied_by_user(
                    planned=planned,
                    request_id=request_id,
                    turn_id=turn_id,
                    note=(decision.note if decision is not None else None),
                )
        if self.tool_runtime is None:
            raise RuntimeError("Tool runtime not initialized.")
        if self.tool_runtime.get_tool(planned.tool_name) is not None:
            return await self._execute_tool(planned=planned, request_id=request_id, turn_id=turn_id)
        if planned.tool_name in mcp_functions:
            return await self._execute_mcp_tool(
                planned=planned,
                fn=mcp_functions[planned.tool_name],
                request_id=request_id,
                turn_id=turn_id,
            )
        return await self._tool_result_error(
            planned=planned,
            request_id=request_id,
            turn_id=turn_id,
            error_code=ErrorCode.TOOL_UNKNOWN.value,
            error_message=f"Unknown tool: {planned.tool_name}",
        )

    async def _execute_mcp_tool(self, *, planned: PlannedToolCall, fn: Any, request_id: str, turn_id: str | None) -> str:
        work_spec_payload = _public_work_spec_from_args(planned.arguments)
        await self._emit(
            kind=EventKind.TOOL_CALL_START,
            payload={
                "tool_execution_id": planned.tool_execution_id,
                "tool_name": planned.tool_name,
                "tool_call_id": planned.tool_call_id,
                "arguments_ref": planned.arguments_ref.to_dict(),
                "summary": f"MCP: {planned.tool_name}",
                "tool_kind": "mcp",
                **({"work_spec": work_spec_payload} if work_spec_payload is not None else {}),
            },
            request_id=request_id,
            turn_id=turn_id,
            step_id=planned.tool_execution_id,
        )

        started = time.monotonic()
        try:
            from agno.run import RunContext
            from agno.tools.function import FunctionCall
            from agno.tools.function import ToolResult as AgnoToolResult
        except Exception as e:
            return await self._tool_result_error(
                planned=planned,
                request_id=request_id,
                turn_id=turn_id,
                error_code=ErrorCode.UNKNOWN.value,
                error_message=f"agno/mcp tooling unavailable: {e}",
            )

        try:
            try:
                fn._run_context = RunContext(run_id=request_id, session_id=self.session_id, metadata={"aura_request_id": request_id, "aura_turn_id": turn_id})
            except Exception:
                logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)
            fc = FunctionCall(function=fn, arguments=dict(planned.arguments), call_id=planned.tool_call_id)
            res = await fc.aexecute()
            if res.status != "success":
                raise RuntimeError(res.error or "MCP tool execution failed")
            raw = res.result
            if isinstance(raw, AgnoToolResult):
                raw_out: Any = {"content": raw.content}
                if raw.images:
                    raw_out["images"] = [img.to_dict() if hasattr(img, "to_dict") else img for img in raw.images]
                raw = raw_out
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            code = _classify_tool_exception(e)
            output_ref = self.artifact_store.put(
                json.dumps({"ok": False, "tool": planned.tool_name, "error_code": code.value, "error": str(e)}, ensure_ascii=False, sort_keys=True, indent=2),
                kind="tool_output",
                meta={"summary": f"{planned.tool_name} output (error)"},
            )
            tool_message = json.dumps(
                {"ok": False, "tool": planned.tool_name, "output_ref": output_ref.to_dict(), "error_code": code.value, "error": str(e), "result": None},
                ensure_ascii=False,
            )
            tool_message_ref = self.artifact_store.put(tool_message, kind="tool_message", meta={"summary": f"{planned.tool_name} tool_result (error)"})
            await self._emit(
                kind=EventKind.TOOL_CALL_END,
                payload={
                    "tool_execution_id": planned.tool_execution_id,
                    "tool_name": planned.tool_name,
                    "tool_call_id": planned.tool_call_id,
                    "summary": f"MCP: {planned.tool_name}",
                    "status": "failed",
                    "duration_ms": duration_ms,
                    "output_ref": output_ref.to_dict(),
                    "tool_message_ref": tool_message_ref.to_dict(),
                    "error_code": code.value,
                    "error": str(e),
                    "tool_kind": "mcp",
                **({"work_spec": work_spec_payload} if work_spec_payload is not None else {}),
                },
                request_id=request_id,
                turn_id=turn_id,
                step_id=planned.tool_execution_id,
            )
            return tool_message

        duration_ms = int((time.monotonic() - started) * 1000)
        output_ref = self.artifact_store.put(
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
        }
        if isinstance(error_code, str) and error_code.strip():
            tool_message_payload["error_code"] = error_code
        if isinstance(error, str) and error.strip():
            tool_message_payload["error"] = error
        tool_message = json.dumps(tool_message_payload, ensure_ascii=False)
        tool_message_ref = self.artifact_store.put(
            tool_message,
            kind="tool_message",
            meta={"summary": f"{planned.tool_name} tool_result ({status})"},
        )
        await self._emit(
            kind=EventKind.TOOL_CALL_END,
            payload={
                "tool_execution_id": planned.tool_execution_id,
                "tool_name": planned.tool_name,
                "tool_call_id": planned.tool_call_id,
                "summary": f"MCP: {planned.tool_name}",
                "status": status,
                "duration_ms": duration_ms,
                "output_ref": output_ref.to_dict(),
                "tool_message_ref": tool_message_ref.to_dict(),
                "error_code": error_code if status != "succeeded" else None,
                "error": error if status != "succeeded" else None,
                "tool_kind": "mcp",
                **({"work_spec": work_spec_payload} if work_spec_payload is not None else {}),
            },
            request_id=request_id,
            turn_id=turn_id,
            step_id=planned.tool_execution_id,
        )
        return tool_message

    def _write_context_ref(self, request: CanonicalRequest) -> ArtifactRef:
        payload = _canonical_request_to_redacted_dict(request)
        return self.artifact_store.put(
            json.dumps(payload, ensure_ascii=False),
            kind="llm_context",
            meta={"summary": "CanonicalRequest (redacted)"},
        )

    async def _emit_llm_response_completed(
        self,
        *,
        final_response: LLMResponse,
        planned_calls: list[PlannedToolCall],
        context_stats: dict[str, Any] | None,
        request_id: str,
        turn_id: str | None,
        step_id: str,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        usage = final_response.usage.__dict__ if final_response.usage is not None else None
        merged_stats: dict[str, Any] = dict(context_stats or {})
        used_tokens = None
        if isinstance(usage, dict) and isinstance(usage.get("input_tokens"), int):
            used_tokens = int(usage["input_tokens"])
            merged_stats["input_tokens"] = used_tokens
            merged_stats["usage_source"] = "provider"
        elif isinstance(merged_stats.get("estimated_input_tokens"), int):
            used_tokens = int(merged_stats["estimated_input_tokens"])
            merged_stats["usage_source"] = "estimate"
        if isinstance(merged_stats.get("context_limit_tokens"), int) and isinstance(used_tokens, int):
            merged_stats["context_left_percent"] = compute_context_left_percent(
                used_tokens=used_tokens,
                context_limit_tokens=int(merged_stats["context_limit_tokens"]),
            )

        assistant_text_raw = final_response.text if isinstance(final_response.text, str) else ""
        assistant_text = assistant_text_raw
        empty_response_fallback = False
        if not assistant_text.strip() and not planned_calls:
            assistant_text = _EMPTY_FINAL_RESPONSE_FALLBACK_TEXT
            empty_response_fallback = True

        thinking_text = final_response.thinking
        thinking_ref = None
        output_ref = self.artifact_store.put(
            assistant_text,
            kind="chat_assistant",
            meta={"summary": _summarize_text(assistant_text)},
        )
        if isinstance(thinking_text, str) and thinking_text.strip():
            thinking_ref = self.artifact_store.put(
                thinking_text,
                kind="llm_thinking",
                meta={"summary": _summarize_text(thinking_text)},
            )
        thought_signatures: dict[str, str] = {}
        for tc in final_response.tool_calls or []:
            tcid = tc.tool_call_id
            sig = tc.thought_signature
            if isinstance(tcid, str) and tcid.strip() and isinstance(sig, str) and sig.strip():
                thought_signatures[tcid.strip()] = sig.strip()
        tool_calls_payload: list[dict[str, Any]] = []
        for p in planned_calls:
            item: dict[str, Any] = {
                "tool_execution_id": p.tool_execution_id,
                "tool_name": p.tool_name,
                "tool_call_id": p.tool_call_id,
                "arguments_ref": p.arguments_ref.to_dict(),
            }
            sig = thought_signatures.get(p.tool_call_id)
            if isinstance(sig, str) and sig:
                item["thought_signature"] = sig
            tool_calls_payload.append(item)
        payload: dict[str, Any] = {
            "profile_id": final_response.profile_id,
            "provider_kind": final_response.provider_kind.value,
            "model": final_response.model,
            "output_ref": output_ref.to_dict(),
            "thinking_ref": (thinking_ref.to_dict() if thinking_ref is not None else None),
            "final_text": assistant_text,
            "tool_calls": tool_calls_payload,
            "usage": usage,
            "context_stats": merged_stats,
            "stop_reason": final_response.stop_reason or ("empty_response" if empty_response_fallback else None),
        }
        if empty_response_fallback:
            payload["response_warning"] = {
                "code": "empty_response_fallback",
                "message": "Provider returned empty assistant text without tool calls.",
            }
        if isinstance(extra_payload, dict):
            payload.update(extra_payload)

        # Non-streaming providers still need to emit deltas for the console UI, which renders
        # assistant text from LLM_RESPONSE_DELTA events (and uses LLM_RESPONSE_COMPLETED as a terminal marker).
        # When streaming is enabled, deltas are emitted incrementally elsewhere and we avoid duplication.
        emit_delta = True
        if isinstance(extra_payload, dict) and extra_payload.get("stream") is True:
            emit_delta = False
        if empty_response_fallback:
            emit_delta = True
        if emit_delta and isinstance(assistant_text, str) and assistant_text:
            chunk_size = 2048
            for i in range(0, len(assistant_text), chunk_size):
                await self._emit(
                    kind=EventKind.LLM_RESPONSE_DELTA,
                    payload={"text_delta": assistant_text[i : i + chunk_size]},
                    request_id=request_id,
                    turn_id=turn_id,
                    step_id=step_id,
                )

        await self._emit(
            kind=EventKind.LLM_RESPONSE_COMPLETED,
            payload=payload,
            request_id=request_id,
            turn_id=turn_id,
            step_id=step_id,
        )

        if isinstance(usage, dict):
            try:
                self.session_store.update_session(self.session_id, {"last_usage": usage, "last_context_stats": merged_stats})
            except Exception:
                logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)

    async def _pause_run(
        self,
        *,
        request_id: str,
        turn_id: str | None,
        planned_calls: list[PlannedToolCall],
        context_stats: dict[str, Any],
        model_profile_id: str | None,
        inspection: Any | None,
        focus_tool_call_id: str | None,
        resume_payload_extra: dict[str, Any] | None = None,
    ) -> str:
        approval_id = new_id("appr")
        action_summary = f"Approve to execute {len(planned_calls)} tool call(s)."
        risk_level = "high"
        reason = "Tool calls require approval."
        diff_ref = None
        if inspection is not None:
            try:
                summary = getattr(inspection, "action_summary", None)
                if isinstance(summary, str) and summary.strip():
                    action_summary = summary.strip()
                    if len(planned_calls) > 1:
                        action_summary = f"{action_summary} (+{len(planned_calls) - 1} more)"
                level = getattr(inspection, "risk_level", None)
                if isinstance(level, str) and level.strip():
                    risk_level = level.strip()
                why = getattr(inspection, "reason", None)
                if isinstance(why, str) and why.strip():
                    reason = why.strip()
                ref = getattr(inspection, "diff_ref", None)
                if ref is not None:
                    try:
                        diff_ref = ref.to_dict()
                    except Exception:
                        diff_ref = None
            except Exception:
                logger.warning("Suppressed exception in _pause_run.", exc_info=True)

        record = ApprovalRecord(
            approval_id=approval_id,
            session_id=self.session_id,
            request_id=request_id,
            created_at=now_ts_ms(),
            status=ApprovalStatus.PENDING,
            turn_id=turn_id,
            action_summary=action_summary,
            risk_level=risk_level,
            options=(
                ["approve", "deny", "edit", "dry_run"]
                if len(planned_calls) == 1 and planned_calls[0].tool_name in _EDITABLE_APPROVAL_TOOLS
                else ["approve", "deny"]
            ),
            reason=reason,
            diff_ref=diff_ref,
            resume_kind="run_continue",
            resume_payload={
                "tool_calls": [
                    {"tool_name": p.tool_name, "tool_call_id": p.tool_call_id, "arguments_ref": p.arguments_ref.to_dict()}
                    for p in planned_calls
                ]
            }
            | (resume_payload_extra or {}),
        )
        self.approval_store.create(record)
        await self._emit(
            kind=EventKind.APPROVAL_REQUIRED,
            payload={
                "approval_id": approval_id,
                "action_summary": record.action_summary,
                "risk_level": record.risk_level,
                "options": record.options,
                "reason": record.reason,
                "diff_ref": record.diff_ref,
                "focus_tool_call_id": focus_tool_call_id,
            },
            request_id=request_id,
            turn_id=turn_id,
            step_id=None,
        )

        snapshot = RunSnapshot(
            schema_version=EVENT_SCHEMA_VERSION,
            run_id=request_id,
            session_id=self.session_id,
            model_profile_id=model_profile_id,
            created_at=now_ts_ms(),
            turn_id=turn_id,
            approval_id=approval_id,
            messages=list(self._history or []),
            pending_tools=[SnapshotPendingToolCall(tool_call_id=p.tool_call_id, tool_name=p.tool_name, args=dict(p.arguments)) for p in planned_calls],
        )
        write_run_snapshot(project_root=self.project_root, snapshot=snapshot)
        await self._emit(
            kind=EventKind.RUN_PAUSED,
            payload={
                "run_id": request_id,
                "approval_id": approval_id,
                "pending_tools": [t.to_dict() for t in snapshot.pending_tools],
                "context_stats": dict(context_stats),
            },
            request_id=request_id,
            turn_id=turn_id,
            step_id=None,
        )
        return approval_id

    async def _execute_tool(self, *, planned: PlannedToolCall, request_id: str, turn_id: str | None) -> str:
        tool = self.tool_runtime.get_tool(planned.tool_name) if self.tool_runtime is not None else None
        if tool is None:
            inspection = self.tool_runtime.inspect(planned) if self.tool_runtime is not None else None
            error_code = inspection.error_code.value if inspection and inspection.error_code is not None else ErrorCode.TOOL_UNKNOWN.value
            error_message = f"Unknown tool: {planned.tool_name}"
            return await self._tool_result_error(
                planned=planned,
                request_id=request_id,
                turn_id=turn_id,
                error_code=error_code,
                error_message=error_message,
            )

        work_spec_payload = _public_work_spec_from_args(planned.arguments)
        start_payload: dict[str, Any] = {
            "tool_execution_id": planned.tool_execution_id,
            "tool_name": planned.tool_name,
            "tool_call_id": planned.tool_call_id,
            "arguments_ref": planned.arguments_ref.to_dict(),
            "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
        }
        if work_spec_payload is not None:
            start_payload["work_spec"] = work_spec_payload

        await self._emit(
            kind=EventKind.TOOL_CALL_START,
            payload=start_payload,
            request_id=request_id,
            turn_id=turn_id,
            step_id=planned.tool_execution_id,
        )

        ctx = ToolExecutionContext(
            session_id=self.session_id,
            request_id=request_id,
            turn_id=turn_id,
            tool_execution_id=planned.tool_execution_id,
            event_bus=self.event_bus,
            metadata={"aura_request_id": request_id, "aura_turn_id": turn_id},
        )

        started = time.monotonic()
        try:
            try:
                from inspect import Parameter, signature

                params = signature(tool.execute).parameters
                accepts_context = "context" in params or any(p.kind is Parameter.VAR_KEYWORD for p in params.values())
            except Exception:
                accepts_context = False

            if accepts_context:
                raw = await asyncio.to_thread(tool.execute, args=planned.arguments, project_root=self.project_root, context=ctx)
            else:
                raw = await asyncio.to_thread(tool.execute, args=planned.arguments, project_root=self.project_root)
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            code = _classify_tool_exception(e)
            output_ref = self.artifact_store.put(
                json.dumps({"ok": False, "tool": planned.tool_name, "error_code": code.value, "error": str(e)}, ensure_ascii=False, sort_keys=True, indent=2),
                kind="tool_output",
                meta={"summary": f"{planned.tool_name} output (error)"},
            )
            tool_message = json.dumps(
                {"ok": False, "tool": planned.tool_name, "output_ref": output_ref.to_dict(), "error_code": code.value, "error": str(e), "result": None},
                ensure_ascii=False,
            )
            tool_message_ref = self.artifact_store.put(tool_message, kind="tool_message", meta={"summary": f"{planned.tool_name} tool_result (error)"})
            end_payload: dict[str, Any] = {
                "tool_execution_id": planned.tool_execution_id,
                "tool_name": planned.tool_name,
                "tool_call_id": planned.tool_call_id,
                "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
                "status": "failed",
                "duration_ms": duration_ms,
                "output_ref": output_ref.to_dict(),
                "tool_message_ref": tool_message_ref.to_dict(),
                "error_code": code.value,
                "error": str(e),
            }
            if work_spec_payload is not None:
                end_payload["work_spec"] = work_spec_payload

            await self._emit(
                kind=EventKind.TOOL_CALL_END,
                payload=end_payload,
                request_id=request_id,
                turn_id=turn_id,
                step_id=planned.tool_execution_id,
            )
            return tool_message

        duration_ms = int((time.monotonic() - started) * 1000)
        output_ref = self.artifact_store.put(
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
        }
        if isinstance(error_code, str) and error_code.strip():
            tool_message_payload["error_code"] = error_code
        if isinstance(error, str) and error.strip():
            tool_message_payload["error"] = error
        tool_message = json.dumps(tool_message_payload, ensure_ascii=False)
        tool_message_ref = self.artifact_store.put(
            tool_message,
            kind="tool_message",
            meta={"summary": f"{planned.tool_name} tool_result ({status})"},
        )

        details = None
        if planned.tool_name in {"project__apply_edits", "project__apply_patch", "project__patch"} and isinstance(raw, dict):
            try:
                details = file_edit_ui_details(
                    diffs=raw.get("diffs") if isinstance(raw.get("diffs"), list) else None,
                    changed_files=raw.get("changed_files") if isinstance(raw.get("changed_files"), list) else None,
                )
            except Exception:
                details = None
        end_payload: dict[str, Any] = {
            "tool_execution_id": planned.tool_execution_id,
            "tool_name": planned.tool_name,
            "tool_call_id": planned.tool_call_id,
            "summary": _summarize_tool_for_ui(planned.tool_name, planned.arguments),
            "status": status,
            "duration_ms": duration_ms,
            "output_ref": output_ref.to_dict(),
            "tool_message_ref": tool_message_ref.to_dict(),
            "error_code": error_code if status != "succeeded" else None,
            "error": error if status != "succeeded" else None,
            "details": details,
        }
        if work_spec_payload is not None:
            end_payload["work_spec"] = work_spec_payload

        await self._emit(
            kind=EventKind.TOOL_CALL_END,
            payload=end_payload,
            request_id=request_id,
            turn_id=turn_id,
            step_id=planned.tool_execution_id,
        )

        if planned.tool_name in {"update_plan", "update_todo", "dag__execute_next"}:
            try:
                if planned.tool_name == "update_todo":
                    state = self.todo_store.get()
                    plan_type = "todo"
                    items = state.todo
                    explanation = state.explanation
                    updated_at = state.updated_at
                else:
                    state = self.plan_store.get()
                    plan_type = "dag"
                    items = state.plan
                    explanation = state.explanation
                    updated_at = state.updated_at
                await self._emit(
                    kind=EventKind.PLAN_UPDATE,
                    payload={
                        "plan_type": plan_type,
                        "plan": [t.to_dict() for t in items],
                        "explanation": explanation,
                        "updated_at": updated_at,
                    },
                    request_id=request_id,
                    turn_id=turn_id,
                    step_id=planned.tool_execution_id,
                )
            except Exception:
                logger.warning("Suppressed exception in _public_work_spec_from_args.", exc_info=True)
        return tool_message

    async def _tool_result_denied(
        self,
        *,
        planned: PlannedToolCall,
        inspection: Any,
        request_id: str,
        turn_id: str | None,
    ) -> str:
        error_code = inspection.error_code.value if getattr(inspection, "error_code", None) is not None else ErrorCode.PERMISSION.value
        error_message = getattr(inspection, "reason", None) or getattr(inspection, "action_summary", None) or "Tool call denied."
        return await self._tool_result_error(
            planned=planned,
            request_id=request_id,
            turn_id=turn_id,
            error_code=error_code,
            error_message=str(error_message),
            status="blocked",
        )

    async def _tool_result_denied_by_user(
        self,
        *,
        planned: PlannedToolCall,
        request_id: str,
        turn_id: str | None,
        note: str | None = None,
    ) -> str:
        note_clean: str | None = None
        if isinstance(note, str) and note.strip():
            note_clean = " ".join(note.strip().splitlines()).strip()
            if len(note_clean) > 400:
                note_clean = note_clean[:399].rstrip() + "…"
        msg = "Approval denied."
        if note_clean:
            msg = f"{msg} User note: {note_clean}"
        return await self._tool_result_error(
            planned=planned,
            request_id=request_id,
            turn_id=turn_id,
            error_code=ErrorCode.CANCELLED.value,
            error_message=msg,
            status="cancelled",
        )

    async def _tool_result_error(
        self,
        *,
        planned: PlannedToolCall,
        request_id: str,
        turn_id: str | None,
        error_code: str,
        error_message: str,
        status: str = "failed",
    ) -> str:
        normalized_status = normalize_tool_end_status(status)
        if normalized_status == "unknown":
            normalized_status = _status_from_error_code(error_code=error_code, fallback="failed")
        output_ref = self.artifact_store.put(
            json.dumps({"ok": False, "tool": planned.tool_name, "error_code": error_code, "error": error_message}, ensure_ascii=False, sort_keys=True, indent=2),
            kind="tool_output",
            meta={"summary": f"{planned.tool_name} output ({normalized_status})"},
        )
        tool_message = json.dumps(
            {"ok": False, "tool": planned.tool_name, "output_ref": output_ref.to_dict(), "error_code": error_code, "error": error_message, "result": None},
            ensure_ascii=False,
        )
        tool_message_ref = self.artifact_store.put(tool_message, kind="tool_message", meta={"summary": f"{planned.tool_name} tool_result ({normalized_status})"})
        payload: dict[str, Any] = {
            "tool_execution_id": planned.tool_execution_id,
            "tool_name": planned.tool_name,
            "tool_call_id": planned.tool_call_id,
            "status": normalized_status,
            "duration_ms": 0,
            "output_ref": output_ref.to_dict(),
            "tool_message_ref": tool_message_ref.to_dict(),
            "error_code": error_code,
            "error": error_message,
        }
        work_spec_payload = _public_work_spec_from_args(planned.arguments)
        if work_spec_payload is not None:
            payload["work_spec"] = work_spec_payload
        raw_status = str(status or "").strip()
        if raw_status and raw_status.lower() != normalized_status:
            payload["status_legacy"] = raw_status
        await self._emit(
            kind=EventKind.TOOL_CALL_END,
            payload=payload,
            request_id=request_id,
            turn_id=turn_id,
            step_id=planned.tool_execution_id,
        )
        return tool_message

    async def _emit(
        self,
        *,
        kind: EventKind,
        payload: dict[str, Any],
        request_id: str | None,
        turn_id: str | None,
        step_id: str | None,
    ) -> Event:
        async with self._event_lock:
            payload_out = dict(payload or {})
            payload_out.setdefault("source", "engine")
            event = Event(
                kind=kind.value,
                payload=payload_out,
                session_id=self.session_id,
                event_id=new_id("evt"),
                timestamp=now_ts_ms(),
                sequence=None,
                request_id=request_id,
                turn_id=turn_id,
                step_id=step_id,
                schema_version=self.schema_version,
            )
            published = self.event_bus.publish(event)
            try:
                seq = int(published.sequence) if isinstance(published.sequence, int) else None
            except Exception:
                seq = None
            if seq is not None:
                try:
                    self.session_store.update_session(
                        self.session_id,
                        {"last_request_id": request_id, "last_event_id": published.event_id, "last_event_sequence": seq},
                    )
                except Exception:
                    logger.warning("Suppressed exception in _emit.", exc_info=True)
            else:
                try:
                    self.session_store.update_session(
                        self.session_id,
                        {"last_request_id": request_id, "last_event_id": published.event_id},
                    )
                except Exception:
                    logger.warning("Suppressed exception in _emit.", exc_info=True)
            return published

    # Agno-backed execution is still used by subagents (see `aura/runtime/subagents/runner.py`),
    # but the main engine LLM path calls Aura's provider adapters directly.
