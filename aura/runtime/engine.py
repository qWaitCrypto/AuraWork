from __future__ import annotations

import asyncio
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .event_bus import EventBus
from .llm.config import ModelConfig
from .llm.errors import CancellationToken
from .protocol import Op
from .stores import ApprovalStore, ArtifactStore, EventLogStore, SessionStore
from .tools.runtime import ToolRuntime


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolDecision:
    tool_call_id: str
    decision: str  # "approve" | "deny"
    note: str | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    status: str  # "completed" | "needs_approval" | "failed" | "cancelled"
    run_id: str
    session_id: str
    approval_id: str | None = None
    pending_tools: list[PendingToolCall] = field(default_factory=list)
    error: str | None = None


@runtime_checkable
class Engine(Protocol):
    """
    Internal execution boundary for Aura runtimes.

    The CLI/UI should depend on this interface, not on a specific backend (legacy vs agno-backed).
    """

    project_root: Path
    session_id: str

    event_bus: EventBus
    session_store: SessionStore
    event_log_store: EventLogStore
    artifact_store: ArtifactStore
    approval_store: ApprovalStore

    model_config: ModelConfig
    tools_enabled: bool
    tool_runtime: ToolRuntime | None
    memory_summary: str | None
    llm_streaming: bool

    def load_history_from_events(self) -> None: ...

    def apply_memory_summary_retention(self) -> None: ...

    def set_chat_model_profile(self, profile_id: str) -> None: ...

    def get_llm_streaming(self) -> bool: ...

    def set_llm_streaming(self, enabled: bool) -> None: ...

    async def arun(
        self,
        op: Op,
        *,
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
    ) -> RunResult: ...

    async def continue_run(
        self,
        *,
        run_id: str,
        decisions: list[ToolDecision],
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
    ) -> RunResult: ...

    def run(
        self,
        op: Op,
        *,
        timeout_s: float | None = None,
        cancel: CancellationToken | None = None,
    ) -> RunResult:
        return asyncio.run(self.arun(op, timeout_s=timeout_s, cancel=cancel))


class EngineBuildError(RuntimeError):
    pass


def build_engine_for_session(
    *,
    project_root: Path,
    session_id: str,
    event_bus: EventBus,
    session_store: SessionStore,
    event_log_store: EventLogStore,
    artifact_store: ArtifactStore,
    approval_store: ApprovalStore,
    model_config: ModelConfig,
    system_prompt: str | None = None,
    tools_enabled: bool = False,
    max_tool_turns: int | None = None,
) -> Engine:
    """
    Factory for constructing the current Aura engine.

    Aura now uses an agno-backed engine. The CLI/UI depends only on the `Engine`
    protocol, so this swap does not affect frontend integrations.
    """

    try:
        from .engine_agno_async import AgnoAsyncEngine
    except Exception as e:
        raise EngineBuildError(f"Failed to initialize async agno engine: {e}") from e

    return AgnoAsyncEngine(
        project_root=project_root,
        session_id=session_id,
        event_bus=event_bus,
        session_store=session_store,
        event_log_store=event_log_store,
        artifact_store=artifact_store,
        approval_store=approval_store,
        model_config=model_config,
        system_prompt=system_prompt,
        tools_enabled=tools_enabled,
        max_tool_turns=30 if max_tool_turns is None else int(max_tool_turns),
    )
